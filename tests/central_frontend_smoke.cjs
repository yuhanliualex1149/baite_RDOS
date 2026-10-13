/* Central UI regression checks use in-memory DOM/API doubles, never live/browser acceptance. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const html = fs.readFileSync(path.join(__dirname, "../server/static/central.html"), "utf8");
const original = fs.readFileSync(path.join(__dirname, "../server/static/central.js"), "utf8");
const autoStart = "  (async()=>{try{const data=await api('/auth/me');";
const index = original.lastIndexOf(autoStart);
assert(index >= 0, "The test must disable auto-start before evaluating UI code.");
const source = original.slice(0, index) + `
  globalThis.centralTest = {state, context, current, refresh, checkOnboarding, renderOnboarding, renderFeedback, renderMessages, renderConnection, renderProjects, renderTaskDrafts, renderProposals, renderDiagnostics, loadDiagnostics, taskTitle, workData, feedbackVersion, prepareInstruction, prepareProject,
    selectProject, clearProject, showProject, showTask, showProposal, startRun, put, close, rememberForm, formAction, resetSession, busy, markdown,
    setAPI(fn){api=fn;},setStartRun(fn){startRun=fn;},setRefresh(fn){refresh=fn;},
    stubRendering(){render=()=>{};renderNavigation=()=>{};renderProjects=()=>{};
      renderOnboarding=()=>{};renderRuns=()=>{};renderActiveRun=()=>{};trackRun=()=>{};}
  };
})();
`;
const tick = () => new Promise((resolve) => setImmediate(resolve));
function environment() {
  const nodes = new Map(), listeners = new Map(), dialogFields = new Map();
  for (const match of html.matchAll(/<dialog\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/dialog>/g)) {
    dialogFields.set(match[1], [...match[2].matchAll(/<(?:input|textarea|select)\b[^>]*id="([^"]+)"/g)].map((m) => m[1]));
  }
  class Node {
    constructor(id, tagName = "DIV") {
      this.id=id;this.tagName=tagName;this.value="";this.checked=false;this.disabled=false;this.open=false;
      this.options=id==="task-nature"?[{value:"每次接入执行",disabled:false}]:[];
      this.textContent="";this.dataset={};this.isConnected=true;this.scrollTop=0;this.scrollHeight=100;this.clientHeight=100;
      this.attributes=[];this.children=[];this.events=new Map();this.classes=new Set();this.type="text";
      this.classList={add:(...items)=>items.forEach(v=>this.classes.add(v)),remove:(...items)=>items.forEach(v=>this.classes.delete(v)),contains:v=>this.classes.has(v),toggle:(v,force)=>{if(force??!this.classes.has(v))this.classes.add(v);else this.classes.delete(v);}};
    }
    get innerHTML(){return this.html||"";}
    set innerHTML(value){
      this.html=value;this.children=[];
      this.checkInputs=[...value.matchAll(/<input[^>]*type="checkbox"[^>]*value="([^"]+)"([^>]*)>/g)].map((m,i)=>{const n=new Node(`${this.id}-check-${i}`,"INPUT");n.value=m[1];n.checked=/\bchecked\b/.test(m[2]);return n;});
      if(this.id==="messages"){
        this.messageCards=[...value.matchAll(/<article class="message /g)].map((_,i)=>{const card=new Node(`message-${i}`);card.owner=this;card.messageIndex=i;return card;});
        this.scrollHeight=this.messageCards.length*120+100;
      }
      const key=value.match(/data-key="([^"]+)"/)?.[1];
      for(const [i,m] of [...value.matchAll(/<details\b([^>]*)>([\s\S]*?)<\/details>/g)].entries()){
        const d=new Node(`${this.id}-detail-${i}`,"DETAILS");d.open=/\bopen\b/.test(m[1]);d.dataset.detail=m[1].match(/data-detail="([^"]+)"/)?.[1];d.owner=this;d.cardKey=key;
        d.summary=new Node(`${d.id}-summary`,"SUMMARY");d.summary.textContent=m[2].match(/<summary[^>]*>([\s\S]*?)<\/summary>/)?.[1]||"";
        this.children.push(d);
      }
    }
    addEventListener(type,callback){const handlers=this.events.get(type)||[];handlers.push(callback);this.events.set(type,handlers);}
    async dispatch(type,extra={}){for(const fn of this.events.get(type)||[])await fn({target:this,currentTarget:this,submitter:node(`${this.id}-submit`,"BUTTON"),preventDefault(){},...extra});}
    querySelectorAll(selector){
      if(selector==="input:checked")return (this.checkInputs||[]).filter(n=>n.checked);
      if(selector===".message")return this.messageCards||[];
      if(selector==="input,textarea,select")return (dialogFields.get(this.id)||[]).map(node);
      if(selector==="button")return [node(`${this.id}-submit`,"BUTTON"),node(`${this.id}-cancel`,"BUTTON")];
      if(selector==="details")return this.children.filter(n=>n.tagName==="DETAILS");
      if(selector==="details[open]")return this.querySelectorAll("details").filter(n=>n.open);
      return [];
    }
    querySelector(selector){if(selector==="summary")return this.summary||null;return null;}
    closest(selector){if(selector==="button")return this.tagName==="BUTTON"?this:null;if(selector==="[data-key]"&&this.cardKey)return {dataset:{key:this.cardKey}};return null;}
    matches(selector){return selector.split(",").some(s=>s.trim().toUpperCase()===this.tagName);}
    contains(other){return other===this||other?.owner===this;}
    getAttribute(name){return name==="id"?this.id:this.attributes.find(a=>a.name===name)?.value||null;}
    setAttribute(name,value){this.removeAttribute(name);this.attributes.push({name,value:String(value)});}
    removeAttribute(name){this.attributes=this.attributes.filter(a=>a.name!==name);}
    toggleAttribute(name,force){if(force??!this.getAttribute(name))this.setAttribute(name,"");else this.removeAttribute(name);}
    focus(){document.activeElement=this;}
    reset(){for(const field of this.querySelectorAll("input,textarea,select")){field.value="";field.checked=false;}}
    replaceChildren(){this.innerHTML="";}
    showModal(){this.open=true;}
    close(){this.open=false;for(const fn of this.events.get("close")||[])fn({target:this});}
    getBoundingClientRect(){return {left:0,right:500,top:this.messageIndex===undefined?0:this.messageIndex*120-this.owner.scrollTop,bottom:500};}
  }
  function node(id, tagName){if(!nodes.has(id))nodes.set(id,new Node(id,tagName));return nodes.get(id);}
  const document={hidden:false,activeElement:null,getElementById:node,
    querySelectorAll(selector){if(selector==="dialog"||selector==="dialog[open]")return [...dialogFields.keys()].map(node).filter(n=>selector==="dialog"||n.open);return [];},
    addEventListener(type,callback){listeners.set(type,callback);}
  };
  const storage=new Map();
  const context=vm.createContext({document,location:{href:"http://localhost:8000/"},window:{addEventListener(){}},URL,AbortController,crypto:webcrypto,
    navigator:{clipboard:{writeText:async()=>{}}},sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
    fetch:async()=>({ok:true,status:200,text:async()=>"{}"}),setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},console});
  vm.runInContext(source,context,{filename:"central-ui.js"});
  const h=context.centralTest;h.stubRendering();h.state.user={id:"u",roles:["admin"],display_name:"测试成员"};h.state.projectId="P";h.state.projects=[{id:"P",name:"测试项目"}];h.state.workspace={project:{id:"P"},runtime:{model_configured:true,feishu_configured:true,paused:false},runs:[],feedback:[]};
  return {h,node,context,listeners};
}

(async()=>{
  let passed=0;
  const test=async(name,fn)=>{await fn(environment());passed++;console.log("PASS "+name);};
  await test("partial refresh failure retains old content and reports unsuccessful update",async({h,node})=>{
    const old=h.state.workspace;h.setAPI(async p=>{if(p==="/projects")return {items:[{id:"P",name:"测试项目"}]};if(p.startsWith("/workspace"))throw new Error("读取失败");if(p.startsWith("/onboarding"))return {};return {items:[]};});
    const result=await h.refresh();assert.equal(result.ok,false);assert.equal(h.state.workspace,old);assert.equal(h.state.errors.workspace,"读取失败");
    await node("refresh-workspace").dispatch("click");assert.match(node("toast").textContent,/未更新/);assert.doesNotMatch(node("toast").textContent,/^已更新/);
  });
  await test("overview refresh never selects the first project implicitly",async({h})=>{
    h.state.projectId=null;const calls=[];
    h.setAPI(async p=>{calls.push(p);if(p==="/projects")return {items:[{id:"P",name:"一"},{id:"Q",name:"二"}]};if(p==="/overview")return {projects:[],tasks:[],feedback:[],runtime:{model_configured:true}};if(p==="/onboarding/status")return {};return {items:[]};});
    await h.refresh();assert.equal(h.state.projectId,null);assert(calls.includes("/overview"));assert(!calls.some(p=>p.startsWith("/workspace")));
  });
  await test("organization chat omits project ID",async({h})=>{
    h.state.overview={runtime:{model_configured:true}};const calls=[];h.setAPI(async(p,o)=>{calls.push({path:p,body:o.body});return {run_id:"run-org"};});
    await h.startRun("/chat",{message:"最近有什么问题",operation_id:"op-1"});
    assert.equal(calls[0].path,"/chat");assert.equal(calls[0].body.project_id,undefined);
    assert.equal(h.state.activeRun.project_id,"");
  });
  await test("top-level work opens all projects while a project card opens only its own work",async({h,node,listeners})=>{
    h.state.projectId="P";h.state.projects=[{id:"P",name:"一"},{id:"Q",name:"二"}];
    const top=node("nav-work","BUTTON");top.dataset.page="work";top.classList.add("page-tab");listeners.get("click")({target:top});
    assert.equal(h.state.projectId,null);assert.equal(h.state.page,"work");
    h.state.projectId="P";h.state.workspaces.P={tasks:[{id:"one",project_id:"P"}]};
    h.state.overview={tasks:[{id:"one",project_id:"P"},{id:"two",project_id:"Q"}]};
    assert.equal(h.workData().tasks.length,1);h.state.projectId=null;assert.equal(h.workData().tasks.length,2);
  });
  await test("new task from all-project view requires a named project",async({h,node})=>{
    h.state.projectId=null;h.state.projects=[{id:"P",name:"一",member_ids:["u"]},{id:"Q",name:"二",member_ids:["u"]}];
    h.state.overview={users:[{id:"u",display_name:"测试成员"}]};h.showTask();
    assert.equal(h.state.taskTargetId,"");assert.equal(node("task-project-label").classList.contains("hidden"),false);
    assert.equal(node("task-member-hint").classList.contains("hidden"),false);
    node("task-project").value="P";await node("task-project").dispatch("change");
    assert.equal(node("task-member-hint").classList.contains("hidden"),true);
    assert.match(node("task-members").innerHTML,/测试成员/);
    node("task-project").value="";await node("task-project").dispatch("change");
    await node("task-form").dispatch("submit");assert.match(node("task-form-error").textContent,/选择任务所属项目/);
  });
  await test("historical chat opens at the latest question and preserves reading position",async({h,node})=>{
    h.state.page="agent";h.state.overview={runs:[],messages:[
      {id:"1",role:"user",content:"旧问题",created_at:"2026-10-01"},
      {id:"2",role:"assistant",content:"旧回答",created_at:"2026-10-02"},
      {id:"3",role:"user",content:"最近的问题",created_at:"2026-10-03"},
      {id:"4",role:"assistant",content:"最近的回答",created_at:"2026-10-04"}
    ]};
    h.renderMessages();const box=node("messages");assert.equal(box.scrollTop,232);
    box.scrollTop=20;h.state.overview.messages.push({id:"5",role:"assistant",content:"后续内容",created_at:"2026-10-05"});
    h.renderMessages();assert.equal(box.scrollTop,20);
  });
  await test("related onboarding task has a readable member title",async({h})=>{
    h.state.overview={users:[{id:"u",display_name:"多多"}]};
    assert.equal(h.taskTitle({task_kind:"onboarding",related_task:"LOCAL-ONB-01",member_ids:["u"],instructions:"已闭环项复用，任务表已核实……"}),"多多的接入补充要求");
  });
  await test("project instruction shows inline progress and ignores duplicate clicks",async({h,node})=>{
    h.state.projects=[{id:"P",name:"测试项目",member_ids:["u"]}];
    let calls=0,release;h.setAPI(()=>{calls++;return new Promise(resolve=>{release=resolve;});});
    const first=h.prepareProject("P"),duplicate=h.prepareProject("P");
    h.renderProjects();assert.equal(calls,1);
    assert.match(node("project-list").innerHTML,/data-project-prompt="P"[^>]*disabled[^>]*aria-busy="true"[^>]*>正在准备/);
    assert.equal(node("project-instruction").classList.contains("hidden"),true);
    release({prompt:"项目说明",url:"http://localhost/guide"});await Promise.all([first,duplicate]);h.renderProjects();
    assert.match(node("project-list").innerHTML,/让我的 Agent 参与项目/);
    assert.equal(node("project-instruction").classList.contains("hidden"),false);
  });
  await test("project preparation A cannot replace newly selected project B",async({h})=>{
    h.state.projects=[{id:"A",name:"A"},{id:"B",name:"B"}];h.state.projectId=null;
    const releases={},calls=[];
    h.setAPI((p,o)=>{calls.push(o.body.project_id);return new Promise(resolve=>{releases[o.body.project_id]=resolve;});});
    const a=h.prepareProject("A"),b=h.prepareProject("B");releases.B({prompt:"B 指令"});await b;
    assert.equal(h.state.projectInstruction.project_id,"B");releases.A({prompt:"A 指令"});await a;
    assert.equal(h.state.projectInstruction.project_id,"B");assert.deepEqual(calls,["A","B"]);
    assert.equal(h.state.projectId,null);
  });
  await test("old onboarding instruction cannot survive a replaced session",async({h})=>{
    h.state.onboarding={session_id:"old-onboard",choice:"new",stage:1};let release;
    h.setAPI(()=>new Promise(resolve=>{release=resolve;}));const pending=h.prepareInstruction(false);
    h.resetSession();h.state.user={id:"another",roles:["admin"]};h.state.projectId="P";
    release({prompt:"旧账号指令",id:"old"});await pending;assert.equal(h.state.instruction,null);
  });
  await test("project changes retain onboarding state and instructions use no project ID",async({h})=>{
    const onboarding={session_id:"onboard-u",choice:"new",stage:1}, instruction={id:"guide-u",prompt:"本步说明"};
    h.state.onboarding=onboarding;h.state.instruction=instruction;h.clearProject();
    assert.equal(h.state.onboarding,onboarding);assert.equal(h.state.instruction,instruction);
    const calls=[];h.setAPI(async(p,o)=>{calls.push({path:p,body:o?.body});return {prompt:"新指令",id:"guide-next"};});
    await h.prepareInstruction(false);
    assert.equal(calls[0].path,"/onboarding/instruction");assert.equal(calls[0].body.project_id,undefined);
  });
  await test("researcher onboarding details omit diagnostic history and source links",({h,node})=>{
    h.state.user.roles=["researcher"];
    h.state.onboarding={session_id:"onboard-u",choice:"new",stage:3,stage_status:"blocked",completed_steps:[0,1],missing:["补充手动执行读回结果"],summary:"本人本轮当前步骤的反馈已读回",evidence:[{summary:"技术依据",source_url:"https://example.com/evidence"}],history:[{stage:0}],sources:{stages:["https://example.com/stage1"]}};
    node("onboarding-mode-label").parentElement=node("onboarding-mode-container");h.renderOnboarding();
    const detail=node("onboarding-check-report").innerHTML;
    assert.match(node("onboarding-status").innerHTML,/补充手动执行读回结果/);assert.match(detail,/下一步/);
    assert.doesNotMatch(detail,/技术依据|example\.com|版本与历史|管理员诊断|本人本轮当前步骤/);
    assert.equal((node("onboarding-progress").innerHTML.match(/<li /g)||[]).length,3);
  });
  await test("failed onboarding read asks to retry instead of inventing a supplement",({h,node})=>{
    h.state.onboarding={session_id:"onboard-u",choice:"refresh",stage:0,stage_status:"blocked",missing:[]};h.state.onboardingError="反馈文档读取失败，请稍后重试。";
    node("onboarding-mode-label").parentElement=node("onboarding-mode-container");h.renderOnboarding();
    assert.equal(node("check-onboarding").textContent,"重试检查");assert(node("copy-onboarding-instruction").classList.contains("hidden"));
    assert.equal(node("prepare-onboarding-instruction").classList.contains("hidden"),true);
    assert.match(node("onboarding-check-error").textContent,/反馈文档读取失败/);
    assert.equal(node("onboarding-action-error").textContent,"");
    assert.doesNotMatch(node("onboarding-status").innerHTML,/需要 Agent 补充/);
  });
  await test("completed onboarding step offers the next guide before asking for another check",async({h,node})=>{
    h.state.onboarding={session_id:"onboard-u",choice:"new",stage:1,stage_status:"pending",completed_steps:[0],missing:[]};
    node("onboarding-mode-label").parentElement=node("onboarding-mode-container");h.renderOnboarding();
    assert.equal(node("continue-onboarding").textContent,"继续：确认工作范围");
    assert.equal(node("continue-onboarding").classList.contains("hidden"),false);
    assert.equal(node("copy-onboarding-instruction").classList.contains("hidden"),true);
    const calls=[];h.setAPI(async(p,o)=>{calls.push({path:p,body:o.body});return {id:"guide-1",prompt:"仅完成当前步骤",url:"https://example.com/guide"};});
    await node("continue-onboarding").dispatch("click");h.renderOnboarding();
    assert.equal(calls.length,1);assert.equal(calls[0].path,"/onboarding/instruction");
    assert.equal(node("continue-onboarding").classList.contains("hidden"),true);
    assert.equal(node("copy-onboarding-instruction").classList.contains("hidden"),false);
    h.state.instruction=null;h.state.onboarding.missing=["尚未收到这一步的反馈"];
    h.renderOnboarding();assert.equal(node("continue-onboarding").classList.contains("hidden"),true);
  });
  await test("a real onboarding omission offers a supplement, while a read failure offers retry",({h,node})=>{
    h.state.onboarding={session_id:"onboard-u",choice:"new",stage:1,stage_status:"blocked",completed_steps:[0],missing:["请确认允许分享的范围"]};
    h.state.instruction={id:"guide-supplement",prompt:"只补缺项"};
    node("onboarding-mode-label").parentElement=node("onboarding-mode-container");h.renderOnboarding();
    assert.equal(node("copy-onboarding-instruction").textContent,"复制补充指令");
    assert.equal(node("copy-onboarding-instruction").classList.contains("hidden"),false);
    assert.equal(node("onboarding-check-error").textContent,"");
    h.state.onboarding.error="反馈读取失败";h.renderOnboarding();
    assert.equal(node("check-onboarding").textContent,"重试检查");
    assert.equal(node("copy-onboarding-instruction").classList.contains("hidden"),true);
    delete h.state.onboarding.error;h.state.onboarding.missing=[];h.renderOnboarding();
    assert.equal(node("check-onboarding").textContent,"重试检查");
    assert.equal(node("copy-onboarding-instruction").classList.contains("hidden"),true);
  });
  await test("onboarding check binds the current session and stage and ignores an earlier refresh",async({h})=>{
    h.state.onboarding={session_id:"run-current",choice:"new",stage:0,stage_status:"pending",completed_steps:[]};
    let releaseStatus,releaseCheck;const calls=[];
    h.setAPI((p,o)=>{calls.push({path:p,body:o?.body});
      if(p==="/projects")return Promise.resolve({items:[{id:"P",name:"测试项目"}]});
      if(p==="/onboarding/status")return new Promise(resolve=>{releaseStatus=resolve;});
      if(p==="/onboarding/check")return new Promise(resolve=>{releaseCheck=resolve;});
      if(p==="/overview")return Promise.resolve({projects:[],tasks:[],feedback:[]});
      return Promise.resolve({items:[]});
    });
    const oldRefresh=h.refresh();await tick();assert.equal(typeof releaseStatus,"function");
    const check=h.checkOnboarding(true);assert.deepEqual(JSON.parse(JSON.stringify(calls.find(c=>c.path==="/onboarding/check").body)),{session_id:"run-current",expected_stage:0});
    releaseCheck({session_id:"run-current",choice:"new",stage:1,stage_status:"pending",completed_steps:[0],missing:[]});await check;
    releaseStatus({session_id:"run-current",choice:"new",stage:0,stage_status:"pending",completed_steps:[]});await oldRefresh;
    assert.equal(h.state.onboarding.stage,1);
  });
  await test("refresh while onboarding check runs does not replace the check result",async({h})=>{
    h.state.onboarding={session_id:"run-current",choice:"new",stage:0,stage_status:"pending",completed_steps:[]};
    let releaseCheck;const calls=[];
    h.setAPI((p,o)=>{calls.push(p);if(p==="/onboarding/check")return new Promise(resolve=>{releaseCheck=resolve;});if(p==="/projects")return Promise.resolve({items:[{id:"P",name:"测试项目"}]});if(p==="/overview")return Promise.resolve({projects:[],tasks:[],feedback:[]});return Promise.resolve({items:[]});});
    const check=h.checkOnboarding(true);await h.refresh();
    assert.equal(calls.includes("/onboarding/status"),false);
    releaseCheck({session_id:"run-current",choice:"new",stage:1,stage_status:"pending",completed_steps:[0],missing:[]});await check;
    assert.equal(h.state.onboarding.stage,1);
  });
  await test("a stale check reloads the current onboarding step",async({h})=>{
    h.state.onboarding={session_id:"run-current",choice:"new",stage:0,stage_status:"pending",completed_steps:[]};
    const calls=[];h.setAPI(async p=>{calls.push(p);if(p==="/onboarding/check"){const e=new Error("接入步骤已变化");e.status=409;throw e;}if(p==="/onboarding/status")return {session_id:"run-current",choice:"new",stage:1,stage_status:"pending",completed_steps:[0],missing:[]};return {};});
    await h.checkOnboarding(true);
    assert.deepEqual(calls,["/onboarding/check","/onboarding/status"]);
    assert.equal(h.state.onboarding.stage,1);assert.equal(h.state.onboardingCheckError,"");
  });
  await test("a prior account's onboarding check cannot update a new login",async({h})=>{
    h.state.onboarding={session_id:"old-run",choice:"new",stage:0,stage_status:"pending",completed_steps:[]};
    let release;h.setAPI(()=>new Promise(resolve=>{release=resolve;}));const pending=h.checkOnboarding(true);
    h.resetSession();h.state.user={id:"other",roles:["researcher"]};
    release({session_id:"old-run",choice:"new",stage:1,stage_status:"pending",completed_steps:[0]});await pending;
    assert.equal(h.state.onboarding,null);
  });
  await test("new account loading clears the previous onboarding instruction status",({h,node})=>{
    node("onboarding-mode-label").parentElement=node("onboarding-mode");
    node("onboarding-action-status").textContent="原指令已过期，重新生成后再复制。";
    h.state.onboarding=null;h.renderOnboarding();
    assert.equal(node("onboarding-action-status").textContent,"");
    assert.equal(node("onboarding-instruction").classList.contains("hidden"),true);
  });
  await test("read-back feedback is received and stable revision ignores read time",({h,node})=>{
    const f={id:"f1",project_id:"P",member_id:"u",content:"完成了资料整理",content_hash:"body-1",source_readback:true,source_url:"https://example.com/feedback",observed_at:"2026-10-11T00:00:00Z"};
    h.state.projectId=null;h.state.overview={feedback:[f],tasks:[],users:[{id:"u",display_name:"测试成员"}]};h.renderFeedback();
    assert.match(node("feedback-list").innerHTML,/已收到反馈/);assert.doesNotMatch(node("feedback-list").innerHTML,/仍待核实/);
    h.state.overview.feedback=[{...f,source_readback:undefined,evidence_status:"正文已从飞书读回；成员身份与工作效果仍按自述记录"}];h.renderFeedback();
    assert.match(node("feedback-list").innerHTML,/已收到反馈/);
    h.state.overview.feedback=[{...f,source_readback:undefined,evidence_status:"Agent自述",content_hash:""}];h.renderFeedback();
    assert.match(node("feedback-list").innerHTML,/尚未读回/);
    assert.equal(h.feedbackVersion(f),h.feedbackVersion({...f,observed_at:"2026-10-12T00:00:00Z"}));
  });
  await test("structured project feedback highlights work and keeps method details optional",({h,node})=>{
    h.state.projectId=null;h.state.overview={tasks:[],users:[{id:"u",display_name:"测试成员"}],feedback:[{
      id:"f-six",project_id:"P",member_id:"u",source_readback:true,source_url:"https://example.com/feedback",
      reported_at:"2026-10-12T09:00:00+08:00",observed_at:"2026-10-13T09:00:00+08:00",period:"2026-10-05 至 2026-10-11 Asia/Shanghai",template_version:"project-feedback-v1",
      task_refs:[{task_id:"TASK-A",task_version:"2",version_status:"当前版本"}],version_status:"当前版本",
      sections:{progress:"完成方案初稿；下周请评审。",outputs:"方案 v2 待评审",blockers:"需要潇斐确认数据",human_gate:"教案 v2 待负责人确认",skill_rag:"读取方法论并实际用于方案",local_skill:"本地调整评审 Skill，尚未共享"},
      content:"原文",content_hash:"same-body"
    }]};h.renderFeedback();
    const html=node("feedback-list").innerHTML;
    assert.match(html,/当前进度[\s\S]*完成方案初稿/);assert.match(html,/本期成果[\s\S]*方案 v2/);
    assert.match(html,/需要协作[\s\S]*潇斐确认/);assert.match(html,/评审与确认线索[\s\S]*负责人确认/);
    assert.match(html,/<details><summary>Skill 与方法使用<\/summary>/);
    assert.match(html,/周期：2026-10-05 至 2026-10-11 Asia\/Shanghai/);
    assert.match(html,/反馈时间：10\/12/);assert.match(html,/系统读取：10\/13/);
    assert.doesNotMatch(html,/版本待核对|旧版本反馈/);
  });
  await test("legacy feedback without report time does not borrow the system read time",({h,node})=>{
    h.state.projectId=null;h.state.overview={tasks:[],users:[{id:"u",display_name:"测试成员"}],feedback:[{
      id:"f-old",project_id:"P",member_id:"u",content:"旧格式项目进展",observed_at:"2026-10-13T09:00:00+08:00",
      task_id:"TASK-OLD",task_version:"1",version_status:"旧版本反馈"
    }]};h.renderFeedback();const html=node("feedback-list").innerHTML;
    assert.match(html,/反馈时间未注明/);assert.match(html,/系统读取：10\/13/);
    assert.match(html,/旧版本反馈/);assert.match(html,/旧格式项目进展/);
    assert.doesNotMatch(html,/反馈时间：10\/13/);
  });
  await test("chat submit preserves newly typed text and prevents duplicate submit",async({h,node})=>{
    let release,writes=0;h.setRefresh(async()=>({ok:true}));h.setStartRun(()=>{writes++;return new Promise(resolve=>{release=resolve;});});
    node("chat-message").value="已发送内容";node("chat-send").disabled=false;
    const first=node("chat-form").dispatch("submit");node("chat-message").value="下一条尚未发送";await node("chat-message").dispatch("input");
    await node("chat-form").dispatch("submit");assert.equal(writes,1);release("run-id");await first;
    assert.equal(node("chat-message").value,"下一条尚未发送");assert.equal(h.state.chatDrafts.organization,"下一条尚未发送");assert.equal(h.state.chatSubmitting,false);
  });
  await test("viewing historical run does not replace the active chat run",async({h,node,listeners})=>{
    h.state.activeRun={id:"active",status:"running",kind:"chat"};h.setAPI(async()=>({run:{id:"history",status:"succeeded"}}));
    const button=node("history-button","BUTTON");button.dataset.run="history";listeners.get("click")({target:button});await tick();
    assert.equal(h.state.activeRun.id,"active");assert.equal(h.state.selectedRun.id,"history");assert.equal(node("workspace-drawer").open,true);
  });
  await test("stale runtime error is not presented as a current chat blocker",({h,node})=>{
    h.state.overview={runtime:{model_configured:true,feishu_configured:true,error:"旧运行失败"},runs:[{id:"recent",status:"succeeded",created_at:"2026-10-11T02:00:00Z"},{id:"old",status:"failed",created_at:"2026-10-11T01:00:00Z"}]};
    h.renderConnection();assert(node("connection-banner").classList.contains("hidden"));
    h.state.overview.runs=[{id:"recent",status:"failed",created_at:"2026-10-11T02:00:00Z"}];h.renderConnection();
    assert.match(node("connection-banner").innerHTML,/上次运行未完成/);assert.match(node("connection-banner").innerHTML,/data-run="recent"/);
  });
  await test("changed proposal source keeps the draft and disables repeat publishing",async({h,node,listeners})=>{
    const item={id:"method-1",project_id:"P",target_id:"doc",target_kind:"docx",summary:"补充依据",old_text:"旧文",new_text:"新文",base_hash:"old-hash",draft_hash:"draft-hash",status:"draft"};
    h.state.overview={tasks:[],proposals:[item],runtime:{model_configured:true,feishu_configured:true}};h.showProposal(item);
    node("proposal-summary").value=item.summary;node("proposal-old").value=item.old_text;node("proposal-new").value=item.new_text;
    let writes=0;h.setAPI(async()=>{writes++;const e=new Error("来源已变化");e.code="source_changed";e.status=409;throw e;});
    listeners.get("click")({target:node("publish-proposal","BUTTON")});await tick();await tick();
    assert.equal(writes,1);assert.equal(h.state.proposal.id,item.id);assert.equal(node("proposal-dialog").open,true);
    assert.match(node("proposal-form-error").textContent,/关闭后请在中央 Agent 中重新读取来源并起草/);
    assert.equal(node("publish-proposal").disabled,true);
    listeners.get("click")({target:node("publish-proposal","BUTTON")});await tick();assert.equal(writes,1);
    h.showProposal(item);assert.match(node("proposal-review").innerHTML,/id="publish-proposal"[^>]*disabled>确认发布/);
    assert.match(node("proposal-form-error").textContent,/重新读取来源并起草/);
    h.showProposal({...item,base_hash:"fresh-hash"});assert.equal(h.state.proposalSourceChanged,null);
  });
  await test("task draft shows action, reason and original version before confirmation",({h,node})=>{
    h.state.projectId=null;h.state.overview={tasks:[],users:[{id:"u",display_name:"测试成员"}],task_drafts:[{id:"d1",project_id:"P",task_id:"TASK-1",draft_action:"update",reason:"调整交付时间",base_task_version:"2",instructions:"交付初稿",member_ids:["u"],status:"draft"}]};
    h.renderTaskDrafts();assert.match(node("task-draft-list").innerHTML,/修改任务/);assert.match(node("task-draft-list").innerHTML,/理由：调整交付时间/);assert.match(node("task-draft-list").innerHTML,/原版本 2/);
    h.showTask(h.state.overview.task_drafts[0],"draft");assert.match(node("task-draft-impact").innerHTML,/基于原版本 2/);assert.equal(node("task-reason").value,"调整交付时间");assert.equal(node("task-submit").textContent,"发布更新");
    h.showTask({...h.state.overview.task_drafts[0],draft_action:"withdraw"},"draft");assert.equal(node("task-instructions").readOnly,true);assert.equal(node("task-reason").readOnly,false);assert.equal(node("task-submit").textContent,"确认停用");
  });
  await test("version conflict preserves task edits and requires a separate re-review",async({h,node})=>{
    const draft={id:"d1",project_id:"P",task_id:"TASK-1",draft_action:"update",reason:"调整要求",base_task_version:"1",instructions:"新要求",member_ids:["u"],task_kind:"work",nature:"一次性",status:"draft",draft_hash:"hash-1"};
    h.state.projectId=null;h.state.overview={tasks:[],users:[{id:"u",display_name:"测试成员"}],task_drafts:[draft]};h.state.projects=[{id:"P",name:"测试项目",member_ids:["u"]}];h.setRefresh(async()=>({ok:true}));h.showTask(draft,"draft");
    let publish=0,edits=0;
    h.setAPI(async(path)=>{if(path.endsWith("/publish")){publish++;if(publish===1){const e=new Error("飞书任务版本已变化");e.code="task_version_changed";e.status=409;throw e;}return {item:{...draft,status:"published"}};}if(path.endsWith("/edit")){edits++;return {item:{...draft,base_task_version:"2",draft_hash:"hash-2"}};}return {items:[]};});
    await node("task-form").dispatch("submit");assert.equal(publish,1);assert.equal(h.state.taskConflict,true);assert.equal(node("task-instructions").value,"新要求");assert.equal(node("task-submit").textContent,"保存草稿并重新审阅");
    await node("task-form").dispatch("submit");assert.equal(edits,1);assert.equal(publish,1);assert.equal(h.state.taskConflict,false);assert.match(node("task-form-status").textContent,/再次确认/);
    await node("task-form").dispatch("submit");assert.equal(publish,2);
  });
  await test("template proposal is local and activation does not claim adoption",({h,node})=>{
    const item={id:"t1",project_id:"P",target_id:"project.feedback",target_kind:"template",source_url:"rdos-template:project.feedback",summary:"简化反馈说明",old_text:"旧说明",new_text:"新说明",status:"published",adoption_status:""};
    h.state.projectId=null;h.state.overview={tasks:[],proposals:[item]};h.renderProposals();assert.match(node("proposal-list").innerHTML,/项目反馈说明（本地模板）/);assert.match(node("proposal-list").innerHTML,/已生效/);assert.match(node("proposal-list").innerHTML,/尚待后续反馈核对/);assert.doesNotMatch(node("proposal-list").innerHTML,/href="rdos-template:/);
    h.showProposal(item);assert.match(node("proposal-review").innerHTML,/所有项目新生成的工作指令/);assert.match(node("proposal-review").innerHTML,/本地模板/);
  });
  await test("administrator diagnosis shows observed evidence and retains last result on failure",async({h,node})=>{
    const data={observed_at:"2026-10-12T01:00:00Z",scope:"本机记录",online:{onboarding:{ready_for_work:1,needs_attention:2},model:{last_success_at:"2026-10-12T01:00:00Z"},tools:{},feishu:{unresolved_operations:1},tasks:{known:3,pending_drafts:1},central_schedule:{}},local_agents:{feedback_readback_records:2,reported_actual_scheduled_trigger:0,evidence_boundary:"仅统计已读回反馈"},limits:["不能据此断定成员没有工作"]};
    h.setAPI(async()=>data);await h.loadDiagnostics();assert.equal(h.state.diagnostics,data);assert.match(node("diagnostics-result").innerHTML,/已读回反馈 2 条/);assert.match(node("diagnostics-result").innerHTML,/仅统计已读回反馈/);
    h.setAPI(async()=>{throw new Error("连接失败")});await h.loadDiagnostics();assert.equal(h.state.diagnostics,data);assert.match(node("diagnostics-error").textContent,/显示上次结果/);
    let calls=0;h.state.user.roles=["researcher"];h.setAPI(async()=>{calls++;return data;});await h.loadDiagnostics();assert.equal(calls,0);
  });
  await test("dirty dialog asks to discard and cancellation retains input",async({h,node})=>{
    node("task-dialog").open=true;h.rememberForm("task-dialog");node("task-instructions").value="未保存要求";h.close("task-dialog");
    assert.equal(node("task-dialog").open,true);assert.equal(node("confirm-dialog").open,true);
    await node("confirm-cancel").dispatch("click");assert.equal(node("task-dialog").open,true);assert.equal(node("task-instructions").value,"未保存要求");
    h.close("task-dialog");await node("confirm-action").onclick();assert.equal(node("task-dialog").open,false);
  });
  await test("busy dialog refuses close and blocks second submission",async({h,node})=>{
    node("task-dialog").open=true;h.rememberForm("task-dialog");let release,writes=0;
    const action=()=>{writes++;return new Promise(resolve=>{release=resolve;});};const pending=h.formAction("task-dialog","task-form-error",action);
    h.close("task-dialog");assert.equal(node("task-dialog").open,true);assert.match(node("task-form-error").textContent,/操作仍在进行/);
    await h.formAction("task-dialog","task-form-error",action);assert.equal(writes,1);release();await pending;
  });
  await test("initial password dialog resists accidental close and explicit close clears secrets",async({h,node})=>{
    node("credential-dialog").open=true;node("credential-password").value="test-only-secret";node("credential-username").value="test-member";
    h.close("credential-dialog");assert.equal(node("credential-dialog").open,true);assert.equal(node("credential-password").value,"test-only-secret");assert.match(node("credential-close-warning").textContent,/先妥善保存/);
    await node("credential-done").dispatch("click");assert.equal(node("credential-dialog").open,false);assert.equal(node("credential-password").value,"");assert.equal(node("credential-username").value,"");
  });
  await test("record refresh preserves expanded details and scroll and reuses identical DOM",({h,node})=>{
    const box=node("detail-box");h.put("detail-box",'<article data-key="run:1"><details data-detail="evidence"><summary>依据</summary><p>旧</p></details></article>');
    box.children[0].open=true;box.scrollTop=83;
    const next='<article data-key="run:1"><details data-detail="evidence"><summary>依据</summary><p>新</p></details></article>';h.put("detail-box",next);
    assert.equal(box.children[0].open,true);assert.equal(box.scrollTop,83);const retained=box.children[0];h.put("detail-box",next);assert.equal(box.children[0],retained);
  });
  await test("changing work scope preserves in-flight feedback and organization chat",({h,node})=>{
    node("sync-feedback").disabled=true;node("sync-feedback").textContent="正在读取…";node("sync-feedback-status").textContent="A 项目仍在读取";node("chat-status").textContent="A 的对话";h.state.syncRun={id:"A-run"};
    h.clearProject();assert.equal(node("sync-feedback-status").textContent,"");assert.equal(node("chat-status").textContent,"A 的对话");assert.equal(h.state.syncRun.id,"A-run");
  });
  await test("logout discards a previous account's source operation and closes its form",({h,node})=>{
    h.state.sourceOperation={id:"old-account-operation"};node("source-form").classList.remove("hidden");node("source-form-error").textContent="旧账号错误";
    h.resetSession();assert.equal(h.state.sourceOperation,null);assert(node("source-form").classList.contains("hidden"));assert.equal(node("source-form-error").textContent,"");
  });
  await test("editing a project with an existing feedback entry does not require a new folder",({h,node})=>{
    h.showProject({id:"P",name:"测试项目",feedback_id:"existing",member_ids:[]});assert.equal(node("project-folder-url").required,false);
    h.showProject(null);assert.equal(node("project-folder-url").required,true);
  });
  await test("Markdown keeps source HTML escaped and refuses script URLs",({h})=>{
    const rendered=h.markdown('<script>alert(1)</script>\n\n[坏链接](javascript:alert)');assert.doesNotMatch(rendered,/<script>/);assert.doesNotMatch(rendered,/href="javascript:/);assert.match(rendered,/&lt;script&gt;/);
  });
  console.log(`CENTRAL FRONTEND PASS: ${passed} isolated regression checks; no live service or browser used.`);
})().catch(error=>{console.error(error);process.exitCode=1;});
