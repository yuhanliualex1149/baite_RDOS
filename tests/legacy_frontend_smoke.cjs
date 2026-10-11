/* Isolated legacy UI regressions: in-memory DOM/API doubles, never browser acceptance. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../server/static/app.js"), "utf8").replace(/\nboot\(\);\s*$/, "\n");
function environment() {
  const nodes = new Map();
  const formFields = {
    "login-form": ["login-username", "login-password"],
    "password-form": ["current-password", "new-password"],
    "runner-form": ["runner-name", "runner-workspace"],
    "project-form": ["project-id", "project-name", "project-description", "project-start", "project-end", "project-cutoff", "project-status", "project-folder", "project-mode"],
    "gate-return-form": ["gate-return-project", "gate-return-record", "gate-return-feedback"],
    "config-form": ["onboarding-platform", "onboarding-text"],
  };
  const dialogForms = { "config-dialog": "config-form", "password-dialog": "password-form", "project-dialog": "project-form", "gate-return-dialog": "gate-return-form" };
  const documentListeners = new Map();
  class Node {
    constructor(id) { this.id = id; this.value = ""; this.checked = false; this.type = id === "rule-rag-required" ? "checkbox" : "text"; this.textContent = ""; this.innerHTML = ""; this.dataset = {}; this.open = false; this.disabled = false; this.isConnected = true; this.events = new Map(); this.classes = new Set(); this.classList = { add: (...a) => a.forEach((v) => this.classes.add(v)), remove: (...a) => a.forEach((v) => this.classes.delete(v)), toggle: (v, force) => { if (force ?? !this.classes.has(v)) this.classes.add(v); else this.classes.delete(v); }, contains: (v) => this.classes.has(v) }; }
    addEventListener(type, callback) { const list = this.events.get(type) || []; list.push(callback); this.events.set(type, list); }
    async dispatch(type) { for (const callback of this.events.get(type) || []) await callback({ target: this, currentTarget: this, preventDefault() {} }); }
    querySelector(selector) {
      if (selector === "form") return node(dialogForms[this.id]);
      if (selector === '[type="submit"]') return node(`${this.id}-submit`);
      if (selector === "[data-form-error]") return node(`${this.id}-error`);
      if (selector === "summary") return node(`${this.id}-summary`);
      return null;
    }
    querySelectorAll(selector) {
      if (selector === "input, textarea, select") return (formFields[this.id] || []).map(node);
      if (selector === "button") return [node(`${this.id}-submit`), node(`${this.id}-cancel`)];
      return [];
    }
    reset() { for (const input of this.querySelectorAll("input, textarea, select")) { input.value = ""; input.checked = false; } }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; for (const callback of this.events.get("close") || []) callback({ target: this }); }
    removeAttribute(name) { delete this[name]; }
    select() {}
  }
  function node(id) { if (!nodes.has(id)) nodes.set(id, new Node(id)); return nodes.get(id); }
  const document = {
    currentScript: { src: "http://localhost/rdos/static/app.js" }, activeElement: null, hidden: false,
    querySelector(selector) { return selector.startsWith("#") ? node(selector.slice(1)) : null; },
    querySelectorAll(selector) {
      if (selector === "dialog" || selector === "dialog[open]") return Object.keys(dialogForms).map(node).filter((item) => selector === "dialog" || item.open);
      if (selector === "form") return Object.keys(formFields).map(node);
      if (selector === "[data-draft-form]") return ["password-form", "project-form", "gate-return-form"].map(node);
      return [];
    },
    addEventListener(type, callback) { documentListeners.set(type, callback); },
  };
  const context = vm.createContext({ document, location: { href: "http://localhost/rdos/legacy" }, window: { addEventListener() {} }, URL, Blob, AbortController, navigator: { clipboard: { writeText: async () => {} } }, confirm: () => true, prompt: () => null, alert() {}, fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }), setTimeout: () => 1, clearTimeout() {}, setInterval() {}, console });
  vm.runInContext(source, context, { filename: "legacy-app.js" });
  const run = (code) => vm.runInContext(code, context);
  const contract = (revision, guidance = "正式正文") => ({ contract_revision: revision, shared_revision: revision, updated_at: "2026-10-11T00:00:00Z", contract: { contract_version: "1", organization_guidance: guidance, knowledge: { selected_rag_required: false }, skills: { shared_skill_policy: "recommended" } } });
  return { node, context, run, contract, documentListeners };
}

(async () => {
  let passed = 0;
  const test = async (name, callback) => { await callback(environment()); passed++; console.log(`PASS ${name}`); };
  await test("refresh preserves all unfocused rule fields and original revision", ({ node, run, contract }) => {
    run(`state.rules = ${JSON.stringify(contract(1))}; renderRules();`);
    node("rule-content").value = "未发布正文"; node("rule-rag-required").checked = true; node("rule-skill-policy").value = "optional";
    run(`state.rules = ${JSON.stringify(contract(2, "另一人发布的新正文"))}; renderRules();`);
    assert.equal(node("rule-content").value, "未发布正文"); assert.equal(node("rule-rag-required").checked, true); assert.equal(node("rule-skill-policy").value, "optional");
    assert.equal(run("state.ruleDraft.revision"), 1); assert.equal(node("save-rules").disabled, true); assert.match(node("rule-edit-status").textContent, /草稿已保留/);
  });
  await test("conflicting revision cannot be published even by invoking handler", async ({ node, run, contract }) => {
    run(`state.rules = ${JSON.stringify(contract(1))}; renderRules();`); node("rule-content").value = "旧草稿";
    run(`state.rules = ${JSON.stringify(contract(2))}; renderRules(); globalThis.writes=0; api=async()=>{writes++;return {};};`);
    await node("save-rules").dispatch("click"); assert.equal(run("writes"), 0);
  });
  await test("rule publish uses editing revision and preserves edits typed during submit", async ({ node, run, contract }) => {
    run(`state.admin={username:'same'}; state.rules=${JSON.stringify(contract(7))}; renderRules(); globalThis.calls=[]; globalThis.resolveSave=null; refreshAll=async()=>{}; api=async(path, options)=>{calls.push(JSON.parse(options.body));return new Promise(resolve=>resolveSave=resolve);};`);
    node("rule-content").value = "本次提交";
    const first = node("save-rules").dispatch("click"); const second = node("save-rules").dispatch("click");
    assert.equal(run("calls.length"), 1); assert.equal(run("calls[0].expected_contract_revision"), 7);
    node("rule-content").value = "提交之后继续编辑";
    run(`resolveSave(${JSON.stringify(contract(8, "本次提交"))});`);
    await Promise.all([first, second]);
    assert.equal(node("rule-content").value, "提交之后继续编辑"); assert.equal(run("rulesDirty()"), true); assert.equal(run("state.ruleDraft.revision"), 8);
  });
  await test("dialog cancel keeps draft until explicit discard and busy close is refused", ({ node, run, context }) => {
    const dialog = node("project-dialog"), form = node("project-form"); dialog.open = true; run("markFormClean(document.querySelector('#project-form')); "); node("project-name").value = "未保存项目";
    context.confirm = () => false; assert.equal(run("closeDialog(document.querySelector('#project-dialog'))"), false); assert.equal(dialog.open, true); assert.equal(node("project-name").value, "未保存项目");
    form.dataset.busy = "1"; context.confirm = () => true; assert.equal(run("closeDialog(document.querySelector('#project-dialog'))"), false);
    form.dataset.busy = "0"; assert.equal(run("closeDialog(document.querySelector('#project-dialog'))"), true); assert.equal(node("project-name").value, "");
  });
  await test("session expiry clears sensitive dialogs while retaining same-account non-password draft", ({ node, run }) => {
    run("state.admin={username:'original'}; markFormClean(document.querySelector('#project-form')); "); node("project-name").value = "保留草稿"; node("new-password").value = "private";
    node("config-dialog").open = true; node("config-json").textContent = "secret"; node("enrollment-code").textContent = "secret"; run("showLogin(true);");
    assert.equal(node("config-dialog").open, false); assert.equal(node("config-json").textContent, ""); assert.equal(node("enrollment-code").textContent, ""); assert.equal(node("new-password").value, ""); assert.equal(node("project-name").value, "保留草稿"); assert.equal(run("state.draftOwner"), "original");
    run("clearAccountData();"); assert.equal(node("project-name").value, ""); assert.equal(run("state.ruleDraft"), null);
  });
  await test("one-time credential close cannot lose secrets by accidental Escape", ({ node, run, context }) => {
    node("config-dialog").open = true; node("enrollment-code").textContent = "private"; context.confirm = () => false;
    assert.equal(run("closeDialog(document.querySelector('#config-dialog'))"), false); assert.equal(node("enrollment-code").textContent, "private");
    context.confirm = () => true; assert.equal(run("closeDialog(document.querySelector('#config-dialog'))"), true); assert.equal(node("enrollment-code").textContent, "");
  });
  await test("stale API result cannot cross session boundary", async ({ run, context }) => {
    let resolve; context.fetch = () => new Promise((done) => { resolve = done; });
    const response = run("api('/api/admin/overview')"); run("showLogin();"); resolve({ ok: true, status: 200, json: async () => ({ secret: "old-account" }) });
    await assert.rejects(response, (error) => error.stale === true);
  });
  await test("project A result cannot replace newly selected project B", async ({ run }) => {
    run("state.admin={username:'admin'}; renderProjects=()=>{}; globalThis.projectResolvers={}; api=(path)=>new Promise(resolve=>projectResolvers[path]=resolve);");
    const a = run("openProject('A')"), b = run("openProject('B')"); run("projectResolvers['/api/admin/projects/B']({item:{id:'B'}});"); await b;
    run("projectResolvers['/api/admin/projects/A']({item:{id:'A'}});"); await a;
    assert.equal(run("state.projectDetail.id"), "B"); assert.equal(run("state.selectedProjectId"), "B");
  });
  await test("failed refresh retains prior data and does not claim empty success", async ({ node, run }) => {
    run("state.admin={username:'admin'}; state.overview={sentinel:'previous'}; state.lastRefresh='2026-10-11T00:00:00Z'; api=async()=>{throw new Error('网络不可用');};");
    await run("refreshAll(true)"); assert.equal(run("state.overview.sentinel"), "previous"); assert.match(node("refresh-status").textContent, /保留.*读取失败/);
    run("state.lastRefresh=null;"); await run("refreshAll(true)"); assert.match(node("refresh-status").textContent, /不能判断是否为空/);
  });
  await test("link protocols and credential-bearing errors remain safe", ({ run }) => {
    assert.equal(run("safeHref('javascript:alert(1)')"), "#"); assert.equal(run("safeHref('https://example.com/doc')"), "https://example.com/doc");
    assert.equal(run("publicError('Bearer veryprivate runner_token=secret-value password=private')").includes("veryprivate"), false); assert.equal(run("publicError('runner_token=secret-value password=private')").includes("secret-value"), false);
  });
  console.log(`LEGACY FRONTEND PASS: ${passed} isolated regression checks; no live service or browser used.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
