const $ = (selector) => document.querySelector(selector);
const basePath = new URL("..", document.currentScript.src).pathname.replace(/\/$/, "");
const state = { admin: null, overview: null, rules: null, contractHistory: [], skills: [], proposals: [], rag: null, projects: [], projectDetail: null, selectedProjectId: null, workflow: null, currentConfig: null };
const titles = { overview: "运行概览", projects: "项目管理", queue: "管理员待办", runners: "Runner 节点", rules: "Operating Contract", skills: "Shared Skills", rag: "Selected RAG" };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function formatTime(value) {
  if (!value) return "—";
  let parsed;
  if (/^\d+$/.test(String(value))) {
    const numeric = Number(value);
    parsed = new Date(numeric < 1e12 ? numeric * 1000 : numeric);
  } else parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? escapeHtml(value) : parsed.toLocaleString("zh-CN", { hour12: false });
}

function toast(message, error = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.add("hidden"), 3500);
}

async function api(path, options = {}) {
  const response = await fetch(basePath + path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login") showLogin();
    const detail = Array.isArray(body.detail) ? body.detail.map((item) => item.msg).join("；") : body.detail;
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return body;
}

function showLogin() {
  state.admin = null;
  if ($("#config-dialog").open) $("#config-dialog").close();
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}

function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#admin-name").textContent = state.admin.username;
}

function empty(text = "暂无需要处理的内容。") { return `<div class="empty">${escapeHtml(text)}</div>`; }
function pill(text, kind = "") { return `<span class="pill ${kind}">${escapeHtml(text)}</span>`; }

function renderOverview() {
  const data = state.overview;
  if (!data) return;
  const online = data.runners.filter((item) => item.online).length;
  const queue = data.work_queue.publish.length + data.work_queue.judgments.length + data.work_queue.incidents.length;
  $("#overview-metrics").innerHTML = [
    ["启用 Runner", data.runners.filter((item) => item.enabled).length, `${online} 个在线`],
    ["进行中项目", (data.projects || []).filter((item) => item.status === "active").length, `${(data.projects || []).filter((item) => ["overdue", "gate_verification"].includes(item.computed_status)).length} 个需关注`],
    ["共享 Revision", data.shared_revision, "Rules / Skills / RAG"],
    ["生效 RAG", data.selected_rag.length, `状态：${data.rag_sync.status}`],
    ["管理员待办", queue, queue ? "需要查看" : "当前已清空"],
  ].map(([label, value, note]) => `<div class="metric"><p class="eyebrow">${label}</p><strong>${value}</strong><span>${escapeHtml(note)}</span></div>`).join("");

  $("#overview-runners").innerHTML = data.runners.length ? `<div class="list-stack">${data.runners.map((runner) => `<div class="list-item"><div class="row"><b><span class="status-dot ${runner.online ? "online" : ""}"></span>${escapeHtml(runner.display_name)}</b>${pill(runner.enabled ? (runner.online ? "Online" : "Offline") : "Disabled", runner.online ? "good" : runner.enabled ? "warn" : "")}</div><p>${escapeHtml(runner.sync_health)} · revision ${runner.last_synced_revision}</p></div>`).join("")}</div>` : empty("还没有 Runner 节点。");

  const withProgress = data.runners.filter((runner) => runner.progress_summary || runner.current_focus);
  $("#overview-progress").innerHTML = withProgress.length ? `<div class="list-stack">${withProgress.map((runner) => `<div class="list-item"><div class="row"><b>${escapeHtml(runner.display_name)}</b>${pill(runner.status)}</div><p><b>${escapeHtml(runner.current_focus || "未填写重点")}</b><br>${escapeHtml(runner.progress_summary)}</p></div>`).join("")}</div>` : empty("Runner 尚未回传日常进度。");

  $("#overview-activities").innerHTML = data.activities.length ? `<div class="list-stack">${data.activities.slice(0, 8).map((item) => `<div class="list-item"><div class="row"><b>${escapeHtml(item.name)}</b>${pill(item.kind)}</div><p>${escapeHtml(item.runner_name)} · ${escapeHtml(item.version || "无版本")}<br>${escapeHtml(item.purpose)}</p></div>`).join("")}</div>` : empty("尚无 Skill / Workflow / Method 元数据。");

  $("#overview-shared").innerHTML = `<div class="list-stack"><div class="list-item"><div class="row"><b>Global Rules</b>${pill(`revision ${data.shared_revision}`, "good")}</div><p>最近发布：${formatTime(state.rules?.updated_at)}</p></div><div class="list-item"><div class="row"><b>Shared Skills</b>${pill(`${data.shared_skills.length} 项`)}</div><p>${data.shared_skills.length ? data.shared_skills.map((item) => `${escapeHtml(item.name)} ${escapeHtml(item.version)}`).join("、") : "暂无正式版本"}</p></div><div class="list-item"><div class="row"><b>飞书 Selected RAG</b>${pill(data.rag_sync.status, data.rag_sync.status === "healthy" ? "good" : "warn")}</div><p>最近成功：${formatTime(data.rag_sync.last_success_at)}</p></div></div>`;
}

function renderQueue() {
  const queue = state.overview?.work_queue;
  if (!queue) return;
  const count = queue.publish.length + queue.judgments.length + queue.incidents.length;
  $("#queue-badge").textContent = count;
  $("#queue-badge").classList.toggle("hidden", !count);
  $("#queue-publish").innerHTML = queue.publish.length ? queue.publish.map((item) => `<div class="queue-item"><div class="row"><b>${escapeHtml(item.skill_name)}</b>${pill(item.proposed_version, "warn")}</div><p>${escapeHtml(item.source_runner_name)}：${escapeHtml(item.summary)}</p><div class="actions"><button class="secondary" data-action="proposal-return" data-id="${item.id}">退回</button><button class="primary" data-action="proposal-publish" data-id="${item.id}">发布</button></div></div>`).join("") : empty();
  $("#queue-judgments").innerHTML = queue.judgments.length ? queue.judgments.map((item) => item.kind === "project_gate"
    ? `<div class="queue-item"><div class="row"><b>${escapeHtml(item.project_name)} · ${escapeHtml(item.gate_code)}</b>${pill("待核实", "warn")}</div><p>${escapeHtml(item.runner_name)} 报告已通过<br>${escapeHtml(item.note || "未填写说明")}</p><div class="actions"><button class="secondary" data-action="project-gate-return" data-project="${item.project_id}" data-id="${item.id}">退回核实</button><button class="primary" data-action="project-gate-confirm" data-project="${item.project_id}" data-id="${item.id}">确认记录</button></div></div>`
    : `<div class="queue-item"><div class="row"><b>${escapeHtml(item.topic)}</b>${pill(item.category, "warn")}</div><p>${escapeHtml(item.from_name)} → ${escapeHtml(item.to_name)}<br>${escapeHtml(item.summary)}</p><div class="actions"><button class="secondary" data-action="collab-return" data-id="${item.id}">退回</button><button class="primary" data-action="collab-confirm" data-id="${item.id}">确认</button></div></div>`).join("") : empty();
  $("#queue-incidents").innerHTML = queue.incidents.length ? queue.incidents.map((item) => `<div class="queue-item"><div class="row"><b>${escapeHtml(item.title)}</b>${pill(`${item.failure_count || 0} 次`, "bad")}</div><p>${escapeHtml(item.message || "需要管理员重新尝试")}</p>${item.kind === "workflow_reference" ? "" : `<div class="actions"><button class="primary" data-action="incident-retry" data-kind="${item.kind}" data-id="${escapeHtml(item.runner_id || "")}" data-project="${escapeHtml(item.project_id || "")}">重新同步</button></div>`}</div>`).join("") : empty();
}

function renderRunners() {
  const runners = state.overview?.runners || [];
  $("#runner-list").innerHTML = runners.length ? runners.map((runner) => `<article class="runner-card"><div class="row"><h3>${escapeHtml(runner.display_name)}</h3>${pill(runner.enabled ? (runner.online ? "Online" : "Offline") : "Disabled", runner.online ? "good" : runner.enabled ? "warn" : "")}</div><p class="mono">${escapeHtml(runner.id)}</p><dl><dt>建议 Workspace</dt><dd>${escapeHtml(runner.workspace_path || "由本机安装器确定")}</dd><dt>实际 Workspace</dt><dd>${escapeHtml(runner.actual_workspace || "尚未回报")}</dd><dt>系统 / 版本</dt><dd>${escapeHtml(runner.platform || "未知")} / ${escapeHtml(runner.runner_version || "未知")}</dd><dt>安装实例</dt><dd>${escapeHtml(runner.installation_id || "尚未使用图形安装器")}</dd><dt>安装自测</dt><dd>${runner.self_test ? `已通过 · ${formatTime(runner.self_test.received_at)}` : "尚未通过"}</dd><dt>最后心跳</dt><dd>${formatTime(runner.last_seen_at)}</dd><dt>同步 Revision</dt><dd>${runner.last_synced_revision}</dd><dt>同步健康</dt><dd>${escapeHtml(runner.sync_health)} · 失败 ${runner.sync_failure_count} 次</dd></dl><div class="actions"><button class="secondary" data-action="runner-onboarding" data-id="${runner.id}">接入信息</button><button class="secondary" data-action="runner-enroll" data-id="${runner.id}">重新签发接入码</button><button class="secondary" data-action="runner-revoke-enrollment" data-id="${runner.id}">撤销未用接入码</button><button class="secondary" data-action="runner-installation" data-id="${runner.id}">查看自测</button><button class="secondary" data-action="runner-rename" data-id="${runner.id}" data-name="${escapeHtml(runner.display_name)}">改名</button><button class="secondary" data-action="runner-toggle" data-id="${runner.id}" data-enabled="${runner.enabled ? "1" : "0"}">${runner.enabled ? "停用" : "启用"}</button><button class="secondary" data-action="runner-rotate" data-id="${runner.id}">轮换 Token</button><button class="secondary" data-action="runner-retry" data-id="${runner.id}">重新同步</button></div></article>`).join("") : empty("还没有节点。点击“创建节点”开始。");
  $("#runner-list").querySelectorAll(".runner-card").forEach((card, index) => {
    const runner = runners[index];
    card.querySelector("dl").insertAdjacentHTML("beforeend", `<dt>Runtime 更新</dt><dd>${escapeHtml(runner.update_state || "旧版节点")} · ${escapeHtml(runner.update_current_version || runner.runner_version || "未知")} → ${escapeHtml(runner.update_target_version || "—")}</dd><dt>更新检查</dt><dd>${formatTime(runner.update_checked_at)}${runner.update_error ? `<br>${escapeHtml(runner.update_error)}` : ""}</dd>`);
    card.querySelector(".actions").insertAdjacentHTML("beforeend", `<button class="secondary" data-action="runner-retry-update" data-id="${runner.id}">重试 Runtime 更新</button>`);
  });
}

function renderRules() {
  if (!state.rules) return;
  const contract = state.rules.contract;
  $("#rule-revision").textContent = `CONTRACT v${contract.contract_version} · 修订 ${state.rules.contract_revision} · SHARED REVISION ${state.rules.shared_revision}`;
  $("#rule-updated").textContent = `最近发布 ${formatTime(state.rules.updated_at)}`;
  if (document.activeElement !== $("#rule-content")) $("#rule-content").value = contract.organization_guidance;
  if (document.activeElement !== $("#rule-rag-required")) $("#rule-rag-required").checked = contract.knowledge.selected_rag_required;
  if (document.activeElement !== $("#rule-skill-policy")) $("#rule-skill-policy").value = contract.skills.shared_skill_policy;
  $("#rule-preview").textContent = JSON.stringify(contract, null, 2);
  $("#rule-history").innerHTML = state.contractHistory.map((item) => `<div class="list-item"><div class="row"><b>修订 ${item.contract_revision}</b>${pill(item.contract_revision === state.rules.contract_revision ? "当前" : "历史")}</div><p>${formatTime(item.updated_at)} · <span class="mono">${escapeHtml(item.contract_hash.slice(0, 12))}…</span></p>${item.contract_revision === state.rules.contract_revision ? "" : `<button class="secondary" data-action="contract-restore" data-id="${item.contract_revision}">恢复为新修订</button>`}</div>`).join("");
}

function renderSkills() {
  $("#skill-list").innerHTML = state.skills.length ? state.skills.map((item) => `<div class="list-item"><div class="row"><b>${escapeHtml(item.name)}</b>${pill(item.version, "good")}</div><p>${escapeHtml(item.description || "暂无说明")}</p><details><summary>查看正式内容</summary><pre>${escapeHtml(item.content)}</pre></details></div>`).join("") : empty("暂无已发布 Shared Skill。");
  $("#proposal-list").innerHTML = state.proposals.length ? state.proposals.map((item) => `<div class="queue-item"><div class="row"><b>${escapeHtml(item.skill_name)}</b>${pill(item.status, item.status === "pending" ? "warn" : item.status === "published" ? "good" : "")}</div><p>${escapeHtml(item.source_runner_name)} · ${escapeHtml(item.base_version || "无基础版本")} → ${escapeHtml(item.proposed_version)}<br>${escapeHtml(item.summary)}</p><details><summary>比较当前与候选内容</summary><div class="diff-grid"><pre>${escapeHtml(item.current_content || "（当前没有正式版本）")}</pre><pre>${escapeHtml(item.content)}</pre></div></details>${item.status === "pending" ? `<div class="actions"><button class="secondary" data-action="proposal-return" data-id="${item.id}">退回</button><button class="primary" data-action="proposal-publish" data-id="${item.id}">发布</button></div>` : item.feedback ? `<p>反馈：${escapeHtml(item.feedback)}</p>` : ""}</div>`).join("") : empty("尚无 Proposal。");
}

function renderRag() {
  if (!state.rag) return;
  const sync = state.rag.state;
  const warning = sync.status !== "healthy";
  $("#rag-state").className = `status-banner${warning ? " warn" : ""}`;
  $("#rag-state").innerHTML = `<b>状态：${escapeHtml(sync.status)}</b>　Snapshot：${escapeHtml(sync.active_snapshot_id || "尚未建立")}<br><span>最近成功：${formatTime(sync.last_success_at)}　连续失败：${sync.failure_count}</span>${sync.last_error ? `<br><span>${escapeHtml(sync.last_error)}</span>` : ""}`;
  $("#rag-files").innerHTML = state.rag.items.length ? state.rag.items.map((item) => { const name = item.source_url && /^https?:/.test(item.source_url) ? `<a href="${escapeHtml(item.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.source_name)}</a>` : escapeHtml(item.source_name); return `<tr><td>${name}<br><span class="muted mono">${escapeHtml(item.runtime_name)}</span></td><td>${escapeHtml(item.source_type)}</td><td>${formatTime(item.modified_time)}</td><td class="mono">${escapeHtml(item.content_hash.slice(0, 12))}…</td><td>${pill("active", "good")}</td></tr>`; }).join("") : `<tr><td colspan="5">${empty("尚无生效文件。")}</td></tr>`;
  $("#rag-ignored").innerHTML = sync.ignored_items?.length ? `<div class="list-stack">${sync.ignored_items.map((item) => `<div class="list-item"><div class="row"><b>${escapeHtml(item.source_name)}</b>${pill(item.status, item.status === "unsupported" ? "warn" : "")}</div><p>类型：${escapeHtml(item.source_type)}</p></div>`).join("")}</div>` : empty("没有未纳入项。");
}

const projectStatusLabels = {
  active: "进行中", paused: "暂停", completed: "已完成",
  not_started: "未开始", due_today: "今日待报", overdue: "已逾期",
  gate_verification: "待 Gate 核实", cycle_ended: "周期结束待结项", on_track: "正常",
};
const reportStatusLabels = { reported: "今日已报", incomplete: "报告不完整", due_today: "今日待报", overdue: "已逾期", not_required: "无需上报" };
const nodeStatusLabels = { not_started: "未开始", in_progress: "进行中", complete: "已完成", blocked: "阻塞", not_applicable: "不适用" };

function statusKind(value) {
  if (["on_track", "reported", "complete", "completed", "healthy", "confirmed"].includes(value)) return "good";
  if (["overdue", "blocked", "returned", "needs_admin"].includes(value)) return "bad";
  return "warn";
}

function catalogLabel(group, value) {
  return state.workflow?.catalog?.[group]?.find((item) => item.value === value)?.label || value;
}

function renderProjects() {
  const projects = state.projects || [];
  const attention = projects.filter((item) => ["overdue", "gate_verification", "cycle_ended"].includes(item.computed_status)).length;
  $("#project-badge").textContent = attention;
  $("#project-badge").classList.toggle("hidden", !attention);
  $("#project-list").innerHTML = projects.length ? `<div class="list-stack">${projects.map((item) => `<button class="project-list-item ${state.selectedProjectId === item.id ? "active" : ""}" data-action="project-select" data-id="${item.id}"><span><b>${escapeHtml(item.name)}</b><small>${escapeHtml(item.start_date)} → ${escapeHtml(item.end_date)}</small></span>${pill(projectStatusLabels[item.computed_status] || item.computed_status, statusKind(item.computed_status))}</button>`).join("")}</div>` : empty("还没有项目。点击“新建项目”开始。");
  renderProjectDetail();
}

function renderProjectDetail() {
  const item = state.projectDetail;
  if (!item) {
    $("#project-detail").innerHTML = `<article class="panel">${empty("请选择或新建一个项目。")}</article>`;
    return;
  }
  const memberNames = item.members.filter((member) => member.active).map((member) => escapeHtml(member.display_name)).join("、") || "暂无";
  const nodesByCode = Object.fromEntries(item.workflow_nodes.map((node) => [node.node_code, node]));
  const nodeCards = (state.workflow?.catalog?.core_nodes || []).map((definition) => {
    const node = nodesByCode[definition.code] || { status: "not_started", evidence: [] };
    const missingEvidence = node.status === "complete" && !node.evidence?.length;
    return `<div class="workflow-node"><div class="row"><b>${definition.code} · ${escapeHtml(definition.name)}</b>${pill(nodeStatusLabels[node.status] || node.status, missingEvidence ? "warn" : statusKind(node.status))}</div><p>${escapeHtml(definition.output)}</p>${node.evidence?.length ? `<small>证据：${node.evidence.map(escapeHtml).join("、")}</small>` : missingEvidence ? `<small class="bad-text">已报告完成，但尚无证据引用</small>` : ""}</div>`;
  }).join("");
  const completedNodes = item.workflow_nodes.filter((node) => node.status === "complete").length;
  const reports = item.runner_reports.map((report) => `<div class="list-item"><div class="row"><b>${escapeHtml(report.display_name)}</b>${pill(reportStatusLabels[report.status] || report.status, statusKind(report.status))}</div><p>${escapeHtml(report.latest_summary || "暂无有效摘要")}<br>最近上报：${formatTime(report.latest_report_at)}</p></div>`).join("");
  const todos = item.todos.length ? item.todos.map((todo) => `<tr><td>${escapeHtml(todo.title)}<br><span class="muted mono">${escapeHtml(todo.todo_id)}</span></td><td>${escapeHtml(todo.owner || "待明确")}</td><td>${pill(todo.status, statusKind(todo.status))}</td><td>${escapeHtml(todo.due_date || "—")}</td><td><button class="text-button" data-action="project-todo-edit" data-id="${escapeHtml(todo.todo_id)}">编辑</button></td></tr>`).join("") : `<tr><td colspan="5">${empty("暂无 Shared TODO。")}</td></tr>`;
  const gates = item.gate_records.length ? item.gate_records.map((gate) => `<div class="queue-item"><div class="row"><b>${escapeHtml(gate.gate_code)} · ${escapeHtml(gate.runner_name)}</b>${pill(gate.status, statusKind(gate.status))}</div><p>${escapeHtml(gate.note || "未填写说明")}<br>证据：${gate.evidence?.length ? gate.evidence.map(escapeHtml).join("、") : "未提供"}${gate.feedback ? `<br>管理员反馈：${escapeHtml(gate.feedback)}` : ""}</p>${gate.status === "reported_passed" ? `<div class="actions"><button class="secondary" data-action="project-gate-return" data-project="${item.id}" data-id="${gate.id}">退回核实</button><button class="primary" data-action="project-gate-confirm" data-project="${item.id}" data-id="${gate.id}">确认记录</button></div>` : ""}</div>`).join("") : empty("暂无 Gate 通过记录。");
  const files = item.content_files.length ? item.content_files.map((file) => `<tr><td>${file.source_url ? `<a href="${escapeHtml(file.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(file.source_name)}</a>` : escapeHtml(file.source_name)}</td><td>${escapeHtml(file.source_type)}</td><td>${formatTime(file.modified_time)}</td><td class="mono">${escapeHtml(file.content_hash.slice(0, 12))}…</td></tr>`).join("") : `<tr><td colspan="4">${empty("尚无已同步正文。")}</td></tr>`;
  const ignored = item.content_snapshot?.ignored || [];
  const ignoredItems = ignored.length ? `<div class="list-stack compact-list">${ignored.map((file) => `<div class="list-item"><div class="row"><b>${file.source_url ? `<a href="${escapeHtml(file.source_url)}" target="_blank" rel="noreferrer">${escapeHtml(file.source_name)}</a>` : escapeHtml(file.source_name)}</b>${pill(file.status, file.status === "unsupported" ? "warn" : "")}</div><small>类型：${escapeHtml(file.source_type)} · 更新：${formatTime(file.modified_time)}</small></div>`).join("")}</div>` : "";
  const latestReports = [];
  const reportRunners = new Set();
  for (const report of (item.reports || [])) { if (!reportRunners.has(report.runner_id)) { latestReports.push(report); reportRunners.add(report.runner_id); } }
  const fileProgress = latestReports.flatMap((report) => {
    const changes = Object.fromEntries((report.file_changes || []).map((change) => [change.path, change.change]));
    const active = (report.file_manifest || []).map((file) => ({ ...file, change: changes[file.path] || "unchanged", runner_name: report.runner_name, created_at: report.created_at }));
    const removed = (report.file_changes || []).filter((change) => change.change === "removed").map((change) => ({ ...change, status: "", summary: "文件已移除", runner_name: report.runner_name, created_at: report.created_at }));
    return [...active, ...removed];
  });
  const changeRows = fileProgress.length ? fileProgress.map((file) => `<tr><td>${escapeHtml(file.path)}</td><td>${pill(file.change, file.change === "removed" ? "bad" : file.change === "added" ? "good" : file.change === "modified" ? "warn" : "")}</td><td>${escapeHtml(file.status || "未标注")}<br><span class="muted">${escapeHtml(file.summary || "")}</span></td><td>${escapeHtml(file.runner_name)}</td><td>${formatTime(file.created_at)}</td></tr>`).join("") : `<tr><td colspan="5">${empty("尚无文件开发状态。")}</td></tr>`;
  const history = (item.audit || []).slice(0, 20).map((entry) => `<div class="timeline-item"><b>${escapeHtml(entry.message)}</b><span>${escapeHtml(entry.actor)} · ${formatTime(entry.created_at)}</span></div>`).join("") || empty("暂无历史记录。");
  $("#project-detail").innerHTML = `
    <article class="panel project-hero"><div class="panel-head"><div><p class="eyebrow">${escapeHtml(item.id)}</p><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.description || "暂无项目说明")}</p></div><div class="actions"><button class="secondary" data-action="project-edit">编辑</button><button class="primary" data-action="project-sync">立即同步飞书资料</button></div></div><div class="project-summary"><div><small>生命周期</small><b>${projectStatusLabels[item.status] || item.status}</b></div><div><small>当前状态</small><b>${projectStatusLabels[item.computed_status] || item.computed_status}</b></div><div><small>周期</small><b>${escapeHtml(item.start_date)} → ${escapeHtml(item.end_date)}</b></div><div><small>参与者</small><b>${memberNames}</b></div></div></article>
    <div class="two-column align-start"><article class="panel"><div class="panel-head"><div><p class="eyebrow">DAILY REPORTS</p><h3>今日上报</h3></div><span>${escapeHtml(item.daily_cutoff)} 截止</span></div><div class="list-stack">${reports || empty()}</div></article><article class="panel"><p class="eyebrow">WORKFLOW PROFILE</p><h3>适用支线</h3><p><b>研发模式：</b>${escapeHtml(catalogLabel("development_modes", item.development_mode))}</p><p><b>产品类型：</b>${item.product_types.map((value) => escapeHtml(catalogLabel("product_types", value))).join("、") || "未选择"}</p><p><b>交付规模：</b>${item.delivery_scales.map((value) => escapeHtml(catalogLabel("delivery_scales", value))).join("、") || "未选择"}</p><p class="muted">用于提示适用检查点，不强制 Local Agent 的执行顺序。</p></article></div>
    <article class="panel project-section"><div class="panel-head"><div><p class="eyebrow">WORKFLOW v0.3</p><h3>研发节点 · 已完成 ${completedNodes} 项</h3></div><a href="${escapeHtml(state.workflow?.catalog?.source_url || "#")}" target="_blank" rel="noreferrer">查看来源</a></div><div class="workflow-grid">${nodeCards}</div></article>
    <article class="panel table-panel project-section"><div class="panel-head"><div><p class="eyebrow">SHARED TODO</p><h3>主要待办与责任人</h3></div><button class="secondary" data-action="project-todo-new">新增 TODO</button></div><div class="table-wrap"><table><thead><tr><th>待办</th><th>责任人</th><th>状态</th><th>截止</th><th></th></tr></thead><tbody>${todos}</tbody></table></div></article>
    <div class="two-column align-start project-section"><article class="panel"><p class="eyebrow">GATE RECORDS</p><h3>Gate 真实性核对</h3><p class="muted">确认或退回只影响记录，不阻断 Runner。</p>${gates}</article><article class="panel"><p class="eyebrow">FEISHU SPACE</p><h3>项目资料同步</h3><p>${pill(item.source_sync_status, statusKind(item.source_sync_status))} · 最近成功 ${formatTime(item.source_last_success_at)}</p><p><a href="${escapeHtml(item.feishu_folder_url)}" target="_blank" rel="noreferrer">打开项目文件夹</a></p><p class="muted">运行时正文 ${item.content_files.length} 项；仅列出 ${ignored.length} 项。Panel 不编辑项目正文。</p>${ignoredItems}${item.source_last_error ? `<p class="bad-text">${escapeHtml(item.source_last_error)}</p>` : ""}</article></div>
    <article class="panel table-panel project-section"><div class="panel-head"><div><p class="eyebrow">PROJECT CONTENT</p><h3>飞书资料快照</h3></div></div><div class="table-wrap"><table><thead><tr><th>文件</th><th>类型</th><th>更新时间</th><th>Hash</th></tr></thead><tbody>${files}</tbody></table></div></article>
    <article class="panel table-panel project-section"><div class="panel-head"><div><p class="eyebrow">FILE PROGRESS</p><h3>本地文件开发状态</h3></div></div><div class="table-wrap"><table><thead><tr><th>相对路径</th><th>变化</th><th>开发状态 / 说明</th><th>Runner</th><th>时间</th></tr></thead><tbody>${changeRows}</tbody></table></div></article>
    <article class="panel project-section"><p class="eyebrow">HISTORY</p><h3>项目记录</h3><div class="timeline">${history}</div></article>`;
}

function checkboxValues(container) {
  return [...document.querySelectorAll(`${container} input:checked`)].map((node) => node.value);
}

function fillProjectForm(item = null) {
  const catalog = state.workflow?.catalog;
  const today = new Date();
  const end = new Date(today); end.setDate(end.getDate() + 30);
  const dateText = (date) => date.toISOString().slice(0, 10);
  $("#project-id").value = item?.id || "";
  $("#project-form-title").textContent = item ? "编辑项目" : "新建项目";
  $("#project-name").value = item?.name || "";
  $("#project-description").value = item?.description || "";
  $("#project-start").value = item?.start_date || dateText(today);
  $("#project-end").value = item?.end_date || dateText(end);
  $("#project-cutoff").value = item?.daily_cutoff || "18:00";
  $("#project-status").value = item?.status || "active";
  $("#project-folder").value = item?.feishu_folder_url || "";
  $("#project-mode").innerHTML = (catalog?.development_modes || []).map((option) => `<option value="${option.value}">${escapeHtml(option.label)}</option>`).join("");
  $("#project-mode").value = item?.development_mode || "new_product";
  const activeMembers = new Set((item?.members || []).filter((member) => member.active).map((member) => member.runner_id));
  $("#project-members").innerHTML = (state.overview?.runners || []).filter((runner) => runner.enabled).map((runner) => `<label class="check-option"><input type="checkbox" value="${runner.id}" ${activeMembers.has(runner.id) ? "checked" : ""} />${escapeHtml(runner.display_name)}</label>`).join("") || empty("请先创建 Runner。 ");
  const selectedTypes = new Set(item?.product_types || []);
  $("#project-types").innerHTML = (catalog?.product_types || []).map((option) => `<label class="check-option"><input type="checkbox" value="${option.value}" ${selectedTypes.has(option.value) ? "checked" : ""} />${escapeHtml(option.label)}</label>`).join("");
  const selectedScales = new Set(item?.delivery_scales || []);
  $("#project-scales").innerHTML = (catalog?.delivery_scales || []).map((option) => `<label class="check-option"><input type="checkbox" value="${option.value}" ${selectedScales.has(option.value) ? "checked" : ""} />${escapeHtml(option.label)}</label>`).join("");
  $("#project-dialog").showModal();
}

function renderAll() { renderOverview(); renderProjects(); renderQueue(); renderRunners(); renderRules(); renderSkills(); renderRag(); }

async function refreshAll(silent = false) {
  try {
    const [overview, rules, history, skills, proposals, rag, projects, workflow] = await Promise.all([
      api("/api/admin/overview"), api("/api/admin/global-contract"), api("/api/admin/global-contract/history"), api("/api/admin/shared-skills"), api("/api/admin/skill-proposals"), api("/api/admin/selected-rag"), api("/api/admin/projects"), api("/api/admin/workflow-reference"),
    ]);
    state.projects = projects.items;
    state.workflow = workflow;
    if (!state.selectedProjectId && state.projects.length) state.selectedProjectId = state.projects[0].id;
    if (state.selectedProjectId && !state.projects.some((item) => item.id === state.selectedProjectId)) state.selectedProjectId = state.projects[0]?.id || null;
    state.projectDetail = state.selectedProjectId ? (await api(`/api/admin/projects/${state.selectedProjectId}`)).item : null;
    Object.assign(state, { overview, rules, contractHistory: history.items, skills: skills.items, proposals: proposals.items, rag });
    renderAll();
  } catch (error) { if (!silent && state.admin) toast(error.message, true); }
}

function selectPage(page) {
  document.querySelectorAll(".page").forEach((item) => item.classList.toggle("active", item.id === `page-${page}`));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  $("#page-title").textContent = titles[page];
}

function showConfig(config, runnerId = config?.runner_id, enrollment = null) {
  state.currentConfig = config;
  state.onboardingRunner = runnerId;
  $("#enrollment-secret").classList.toggle("hidden", !enrollment);
  $("#enrollment-code").textContent = enrollment?.enrollment_code || "";
  $("#enrollment-expiry").textContent = enrollment ? `有效至 ${formatTime(enrollment.expires_at)}` : "";
  $("#config-secret").classList.toggle("hidden", !config);
  $("#config-unavailable").classList.toggle("hidden", !!config);
  $("#config-json").textContent = config ? JSON.stringify(config, null, 2) : "";
  if (state.configUrl) URL.revokeObjectURL(state.configUrl);
  state.configUrl = config ? URL.createObjectURL(new Blob([JSON.stringify(config, null, 2)], { type: "application/json" })) : null;
  $("#download-config").href = state.configUrl || "#";
  $("#download-config").download = `runner-${runnerId}.json`;
  $("#config-dialog").showModal();
  loadOnboarding();
}

async function loadOnboarding() {
  const runnerId = state.onboardingRunner;
  const platform = $("#onboarding-platform").value;
  const requestId = state.onboardingRequest = (state.onboardingRequest || 0) + 1;
  $("#onboarding-text").value = "";
  $("#copy-onboarding").disabled = true;
  $("#download-bootstrapper").classList.add("hidden");
  try {
    const result = await api(`/api/admin/runners/${runnerId}/onboarding?platform=${platform}`);
    if (requestId !== state.onboardingRequest || !$("#config-dialog").open) return;
    $("#onboarding-text").value = result.instructions;
    $("#copy-onboarding").disabled = false;
    if (result.installer_url) {
      $("#download-bootstrapper").href = result.installer_url;
      $("#download-bootstrapper").classList.remove("hidden");
    }
  } catch (error) { toast(error.message, true); }
}

$("#copy-enrollment").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#enrollment-code").textContent); toast("接入码已复制，请私下交付"); }
  catch { toast("浏览器不允许复制，请手动复制接入码", true); }
});

$("#onboarding-platform").addEventListener("change", loadOnboarding);
$("#copy-onboarding").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("#onboarding-text").value); toast("接入指令已复制，不含 Token"); }
  catch { $("#onboarding-text").select(); toast("浏览器未允许剪贴板访问，请复制已选中的指令"); }
});
$("#config-dialog").addEventListener("close", () => {
  state.currentConfig = null; state.onboardingRunner = null; state.onboardingRequest++;
  if (state.configUrl) URL.revokeObjectURL(state.configUrl);
  state.configUrl = null;
  $("#config-json").textContent = "";
  $("#onboarding-text").value = "";
  $("#enrollment-code").textContent = "";
  $("#enrollment-secret").classList.add("hidden");
  $("#download-config").removeAttribute("href");
});

async function proposalDecision(id, decision) {
  let feedback = "";
  if (decision === "return") {
    feedback = prompt("请填写退回反馈：") || "";
    if (!feedback.trim()) return;
  }
  await api(`/api/admin/skill-proposals/${id}/decision`, { method: "POST", body: JSON.stringify({ decision, feedback }) });
  toast(decision === "publish" ? "Shared Skill 已发布并进入新 revision" : "Proposal 已退回");
  await refreshAll();
}

async function collaborationDecision(id, decision) {
  let feedback = "";
  if (decision === "return") {
    feedback = prompt("请填写退回反馈：") || "";
    if (!feedback.trim()) return;
  }
  await api(`/api/admin/collaborations/${id}/decision`, { method: "POST", body: JSON.stringify({ decision, feedback }) });
  toast("协作判断已同步");
  await refreshAll();
}

async function projectGateDecision(projectId, id, decision) {
  if (decision === "return") {
    $("#gate-return-project").value = projectId;
    $("#gate-return-record").value = id;
    $("#gate-return-feedback").value = "";
    $("#gate-return-dialog").showModal();
    $("#gate-return-feedback").focus();
    return;
  }
  await api(`/api/admin/projects/${projectId}/gate-records/${id}/decision`, { method: "POST", body: JSON.stringify({ decision, feedback: "" }) });
  toast("Gate 通过记录已确认；不会改变 Runner 工作权限");
  await refreshAll();
}

async function openProject(projectId) {
  state.selectedProjectId = projectId;
  state.projectDetail = (await api(`/api/admin/projects/${projectId}`)).item;
  renderProjects();
}

async function editTodo(todoId = "") {
  const current = state.projectDetail?.todos.find((item) => item.todo_id === todoId);
  const title = prompt("TODO 内容：", current?.title || "") || "";
  if (!title.trim()) return;
  const owner = prompt("责任人（自由文本）：", current?.owner || "") ?? "";
  const status = prompt("状态：open / in_progress / blocked / done", current?.status || "open") || "open";
  if (!["open", "in_progress", "blocked", "done"].includes(status)) throw new Error("TODO 状态不正确");
  const due_date = prompt("截止日期 YYYY-MM-DD（可留空）：", current?.due_date || "") || null;
  const id = todoId || `todo-${Date.now()}`;
  await api(`/api/admin/projects/${state.selectedProjectId}/todos/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ title: title.trim(), owner: owner.trim(), status, due_date }) });
  toast("Shared TODO 已更新");
  await refreshAll();
}

document.addEventListener("click", async (event) => {
  const pageButton = event.target.closest("[data-page]");
  if (pageButton) selectPage(pageButton.dataset.page);
  const button = event.target.closest("[data-action]");
  if (!button) return;
  button.disabled = true;
  try {
    const id = button.dataset.id;
    switch (button.dataset.action) {
      case "contract-restore": {
        if (!confirm(`将修订 ${id} 的内容恢复为新的正式修订？`)) break;
        await api(`/api/admin/global-contract/history/${id}/restore`, { method: "POST", body: JSON.stringify({ expected_contract_revision: state.rules.contract_revision }) });
        toast("历史内容已作为新修订发布"); await refreshAll(); break;
      }
      case "proposal-publish": await proposalDecision(id, "publish"); break;
      case "proposal-return": await proposalDecision(id, "return"); break;
      case "collab-confirm": await collaborationDecision(id, "confirm"); break;
      case "collab-return": await collaborationDecision(id, "return"); break;
      case "runner-rename": {
        const display_name = prompt("新的显示名：", button.dataset.name);
        if (display_name?.trim()) await api(`/api/admin/runners/${id}`, { method: "PATCH", body: JSON.stringify({ display_name: display_name.trim() }) });
        await refreshAll(); break;
      }
      case "runner-toggle": await api(`/api/admin/runners/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: button.dataset.enabled !== "1" }) }); await refreshAll(); break;
      case "runner-rotate": { const result = await api(`/api/admin/runners/${id}/rotate-token`, { method: "POST", body: "{}" }); showConfig(result.config); await refreshAll(); break; }
      case "runner-onboarding": showConfig(null, id); break;
      case "runner-enroll": {
        if (!confirm("重新签发接入码？旧接入码立即失效；原 Runner Token 在新设备认领前继续有效。")) break;
        const enrollment = await api(`/api/admin/runners/${id}/enrollment`, { method: "POST", body: "{}" });
        showConfig(null, id, enrollment); break;
      }
      case "runner-revoke-enrollment": {
        if (!confirm("撤销该节点尚未使用的接入码？已认领的 Runner 凭证不会受影响。")) break;
        await api(`/api/admin/runners/${id}/enrollment`, { method: "DELETE" });
        toast("未使用的接入码已撤销"); break;
      }
      case "runner-installation": {
        const detail = await api(`/api/admin/runners/${id}/installation`);
        const test = detail.self_test;
        alert(test ? `安装自测已收到：${test.event_id}\n接收时间：${formatTime(test.received_at)}\n共享 revision：${test.shared_revision}\n项目：${test.project_count}` : "尚未收到此安装实例的自测结果");
        break;
      }
      case "runner-retry": await api(`/api/admin/runners/${id}/retry-sync`, { method: "POST", body: "{}" }); toast("已允许 Runner 重新同步"); await refreshAll(); break;
      case "runner-retry-update": await api(`/api/admin/runners/${id}/retry-update`, { method: "POST", body: "{}" }); toast("已允许 Manager 重新检查更新"); await refreshAll(); break;
      case "project-select": await openProject(id); break;
      case "project-edit": fillProjectForm(state.projectDetail); break;
      case "project-sync": await api(`/api/admin/projects/${state.selectedProjectId}/sync`, { method: "POST", body: "{}" }); toast("项目飞书资料同步完成"); await refreshAll(); break;
      case "project-todo-new": await editTodo(); break;
      case "project-todo-edit": await editTodo(id); break;
      case "project-gate-confirm": await projectGateDecision(button.dataset.project, id, "confirm"); break;
      case "project-gate-return": await projectGateDecision(button.dataset.project, id, "return"); break;
      case "incident-retry":
        if (button.dataset.kind === "selected_rag") await synchronizeRag();
        else if (button.dataset.kind === "project_source") { await api(`/api/admin/projects/${button.dataset.project}/sync`, { method: "POST", body: "{}" }); await refreshAll(); }
        else if (button.dataset.kind === "project_export") { await api(`/api/admin/projects/${button.dataset.project}/retry-writeback?runner_id=${encodeURIComponent(id)}`, { method: "POST", body: "{}" }); await refreshAll(); }
        else { await api(`/api/admin/runners/${id}/retry-sync`, { method: "POST", body: "{}" }); await refreshAll(); }
        break;
    }
  } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try {
    const result = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username: $("#login-username").value, password: $("#login-password").value }) });
    state.admin = result.admin; $("#login-password").value = ""; showApp(); await refreshAll();
  } catch (error) { $("#login-error").textContent = error.message; }
});

$("#gate-return-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = $("#gate-return-project").value;
  const recordId = $("#gate-return-record").value;
  const feedback = $("#gate-return-feedback").value.trim();
  if (!feedback) return;
  try {
    await api(`/api/admin/projects/${projectId}/gate-records/${recordId}/decision`, { method: "POST", body: JSON.stringify({ decision: "return", feedback }) });
    $("#gate-return-dialog").close();
    toast("已退回核实；不会阻断 Runner 工作");
    await refreshAll();
  } catch (error) { toast(error.message, true); }
});

$("#cancel-gate-return").addEventListener("click", () => $("#gate-return-dialog").close());

$("#logout").addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST", body: "{}" }); } finally { showLogin(); } });
$("#show-runner-form").addEventListener("click", () => $("#runner-form").classList.toggle("hidden"));
$("#runner-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/admin/runners", { method: "POST", body: JSON.stringify({ display_name: $("#runner-name").value, workspace: $("#runner-workspace").value, delivery: "enrollment" }) });
    showConfig(null, result.item.id, result.enrollment); event.target.reset(); event.target.classList.add("hidden"); await refreshAll();
  } catch (error) { toast(error.message, true); }
});

$("#show-project-form").addEventListener("click", () => fillProjectForm());
$("#cancel-project").addEventListener("click", () => $("#project-dialog").close());
$("#project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = $("#project-id").value;
  const payload = {
    name: $("#project-name").value.trim(),
    description: $("#project-description").value.trim(),
    participant_runner_ids: checkboxValues("#project-members"),
    start_date: $("#project-start").value,
    end_date: $("#project-end").value,
    daily_cutoff: $("#project-cutoff").value,
    timezone: "Asia/Shanghai",
    feishu_folder_url: $("#project-folder").value.trim(),
    development_mode: $("#project-mode").value,
    product_types: checkboxValues("#project-types"),
    delivery_scales: checkboxValues("#project-scales"),
    status: $("#project-status").value,
  };
  try {
    if (!payload.participant_runner_ids.length) throw new Error("至少选择一个参与 Runner");
    const result = await api(projectId ? `/api/admin/projects/${projectId}` : "/api/admin/projects", { method: projectId ? "PATCH" : "POST", body: JSON.stringify(payload) });
    state.selectedProjectId = result.item.id;
    $("#project-dialog").close();
    toast(projectId ? "项目设置已更新" : "项目已创建，资料同步已排队");
    await refreshAll();
  } catch (error) { toast(error.message, true); }
});

$("#save-rules").addEventListener("click", async () => {
  try {
    await api("/api/admin/global-contract", { method: "PUT", body: JSON.stringify({
      organization_guidance: $("#rule-content").value,
      selected_rag_required: $("#rule-rag-required").checked,
      shared_skill_policy: $("#rule-skill-policy").value,
      expected_contract_revision: state.rules.contract_revision,
    }) });
    toast("Operating Contract 已发布并同步"); await refreshAll();
  } catch (error) { toast(error.message, true); }
});

async function synchronizeRag() {
  const button = $("#sync-rag"); button.disabled = true; button.textContent = "同步中…";
  try { const result = await api("/api/admin/selected-rag/sync", { method: "POST", body: "{}" }); toast(result.changed ? "已生成并切换新 RAG Snapshot" : "同步完成，内容没有变化"); await refreshAll(); } catch (error) { toast(error.message, true); await refreshAll(true); } finally { button.disabled = false; button.textContent = "立即同步"; }
}
$("#sync-rag").addEventListener("click", synchronizeRag);

$("#change-password").addEventListener("click", () => $("#password-dialog").showModal());
$("#cancel-password").addEventListener("click", () => $("#password-dialog").close());
$("#password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { await api("/api/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: $("#current-password").value, new_password: $("#new-password").value }) }); $("#password-dialog").close(); showLogin(); toast("密码已修改，请重新登录"); } catch (error) { toast(error.message, true); }
});

async function boot() {
  try { const result = await api("/api/auth/me"); state.admin = result.admin; showApp(); await refreshAll(); } catch (_) { showLogin(); }
  setInterval(() => { if (state.admin && !document.hidden) refreshAll(true); }, 5000);
}
boot();
