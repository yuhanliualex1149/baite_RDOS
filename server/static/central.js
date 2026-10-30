(() => {
  "use strict";
  let labInfo = null;
  const developerMode = location.pathname?.replace(/\/$/, "") === "/dev";
  document.body?.classList.toggle("developer-mode", developerMode);
  const labStoragePrefix = () => ['rdos-lab', labInfo?.env_id || 'unverified', labInfo?.generation || 'unverified'].join(':');
  const $ = (id) => document.getElementById(id);
  const list = (v) => Array.isArray(v) ? v : [];
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const labels = { admin:"管理员", researcher:"研发成员", actual:"实际身份", role_test:"角色测试", active:"有效", inactive:"已停用", deactivating:"停用处理中", published:"已发布", draft:"待审阅", rejected:"已退回", pending:"待核对", uncertain:"待核对", needs_attention:"待处理", unchanged:"无变化未分析", recovered:"已恢复", healthy:"正常", ready:"尚无异常", backoff:"等待下次检查", blocked:"需要补充", withdrawn:"已撤回", running:"运行中", queued:"等待运行", succeeded:"已完成", completed:"已完成", failed:"失败", denied:"已拒绝", interrupted:"运行中断", cancelled:"已取消", paused:"已暂停", verified:"可核验结果", self_reported:"自述", unverified:"尚未验证", current:"当前版本", stale:"旧版本反馈", unknown:"待核对" };
  const label = (v) => labels[v] || String(v || "待核对");
  const staleProposalMessage = '来源已变化，当前草稿保留。关闭后请在中央 Agent 中重新读取来源并起草。';
  const proposalStaleMessage = item => item?.target_kind==='template'?'指令模板已有新版本，草稿保留。请根据当前模板重新起草后审阅。':staleProposalMessage;
  const pill = (v, tone = "") => '<span class="pill ' + tone + '">' + esc(label(v)) + '</span>';
  const state = { user:null, workspace:null, workspaces:{}, overview:null, page:developerMode?"work":"projects", tab:"tasks", projectId:null, threadId:null, threads:[], threadSelections:{}, runItems:[], runNext:null, runTotal:0, runsLoading:false, projects:[], users:[], nodes:[], defaultFolderUrl:"", projectInitialized:false, showInactive:false, projectActionOps:{}, syncResults:{}, sources:[], onboarding:null, onboardingError:"", onboardingCheckError:"", onboardingRequestSeq:0, instructionError:"", reentering:false, onboardingStarting:false, onboardingStartSeq:0, instruction:null, instructionPromise:null, projectInstruction:null, checking:false, copied:false, lastCheck:0, activeRun:null, selectedRun:null, task:null, taskMode:"manual", taskConflict:false, editUser:null, editProject:null, proposal:null, proposalEditing:false, proposalSourceChanged:null, diagnostics:null, diagnosticsLoading:false, diagnosticsError:"", diagnosticsSeq:0, polling:new Map(), refreshTimer:null, refreshPromise:null, toastTimer:null, generation:0, session:0, refreshSeq:0, instructionSeq:0, projectInstructionSeq:0, errors:{}, loaded:{}, chatOperation:null, chatSubmitting:false, chatPending:{}, chatComposing:false, syncRun:null, chatDrafts:{}, forms:new Map(), taskOperation:null, projectOperation:null, sourceOperation:null, publishOperation:null };
  const knowledge = {data:null, category:'selected_rag', categoryExpanded:true, selectedId:null, detail:null, fileId:null, fileDetail:null, fileError:'', fileSequence:0, error:'', loading:false, syncing:false, sequence:0, detailSequence:0, operations:{}};
  const phase3 = {data:null,error:'',loading:false,sequence:0,task:null,draft:null,taskOperation:null,action:null,syncOperation:null};
  const rodos = {policy:null,memories:[],preview:null,maintenance:null,runs:[],nodes:[],loading:false,settingsLoaded:false,policyDirty:false,memoryDirty:false,edit:null,tab:'agent',sequence:0,policyOp:null,memoryOp:null,maintenanceOps:{},policyConflict:null};
  const navigation = {projectId:null,collaboration:'tasks',settings:'memory',showClosed:false};
  const context = () => ({session:state.session, generation:state.generation, projectId:state.projectId, threadId:state.threadId});
  const current = c => !!state.user && c.session===state.session && c.generation===state.generation && c.projectId===state.projectId && c.threadId===state.threadId;
  const sameSession = c => !!state.user && c.session===state.session;
  const onboardingContext = () => ({session:state.session});
  const currentOnboarding = c => sameSession(c);
  const displayName = u => String(u?.display_name||u?.username||"本人").replace(/（本地角色测试）$/, "");
  const projectName = id => state.projects.find(p=>p.id===id)?.name || "未知项目";
  const setText = (id,text="") => {const node=$(id);if(!node)return;node.textContent=text;
    // Status text may have been hidden while switching projects; a later
    // result must be visible again, especially when an instruction expires.
    if(node.getAttribute('role')==='status')node.classList.toggle('hidden',!text);
  };
  const show = (id,visible=true) => $(id)?.classList.toggle("hidden",!visible);
  const readableError = value => {const t=typeof value==='string'?value:'';return /[\u3400-\u9fff]/.test(t)?t:'本次处理未完成，请重试或查看执行详情。';};
  const expired = instruction => !!instruction?.expires_at && new Date(instruction.expires_at).getTime()<=Date.now();
  // Reuse unchanged DOM; preserve expanded evidence and focus when records update.
  function put(id, html) {
    const node=$(id); if(!node || node.dataset.rendered===html)return;
    if(node.contains(document.activeElement)&&document.activeElement.matches("input,textarea,select"))return;
    const key=d=>(d.closest("[data-key]")?.dataset.key||id)+":"+(d.dataset.detail||d.querySelector("summary")?.textContent||"");
    const opened=new Set([...node.querySelectorAll("details[open]")].map(key));
    const focus=node.contains(document.activeElement)?document.activeElement:null;
    const focusId=focus?.id, focusData=focus?[...focus.attributes].find(a=>a.name.startsWith("data-")):null;
    const focusDetail=focus?.tagName==='SUMMARY'?key(focus.parentElement):null;
    const scroll=node.scrollTop;
    node.innerHTML=html;node.dataset.rendered=html;
    node.querySelectorAll("details").forEach(d=>{if(opened.has(key(d)))d.open=true;});
    node.scrollTop=scroll;
    if(focusDetail)[...node.querySelectorAll("details")].find(d=>key(d)===focusDetail)?.querySelector("summary")?.focus({preventScroll:true});
    else if(focusId)$(focusId)?.focus({preventScroll:true});
    else if(focusData)[...node.querySelectorAll("button")].find(b=>b.getAttribute(focusData.name)===focusData.value)?.focus({preventScroll:true});
  }
  const admin = () => list(state.user?.roles).includes("admin");
  const uid = () => {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const h = [...bytes].map(v => v.toString(16).padStart(2, "0")).join("");
    return h.slice(0,8)+"-"+h.slice(8,12)+"-"+h.slice(12,16)+"-"+h.slice(16,20)+"-"+h.slice(20);
  };
  const running = (run) => ["running","queued","pending"].includes(run?.status);
  const withdrawn = (task) => ["withdrawn","停用","已撤回"].includes(task.status);
  const safeUrl = (v) => {
    if (!v || typeof v !== "string") return null;
    try { const u = new URL(v, location.href); return ["http:","https:"].includes(u.protocol) && u.origin === location.origin ? u.href : null; } catch { return null; }
  };
  const link = (url, title = "打开原文 ↗") => {
    const safe=safeUrl(url);if(!safe)return '';
    const path=new URL(safe).pathname;
    if(/^\/(?:docx|file|base|drive\/folder)\//.test(path))return '<span class="small-note" title="本地模拟来源标识；正文请查看读取结果或执行详情">本地模拟来源 · '+esc(path.split('/').filter(Boolean).pop())+'</span>';
    return '<a href="'+esc(safe)+'" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">'+esc(title)+'</a>';
  };
  const safeJSON = (v) => JSON.stringify(v ?? null, (key, item) => /(?:password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|credential)/i.test(key) ? "[已隐藏]" : item, 2);
  const textValue = (v) => typeof v === "string" ? v : safeJSON(v);
  const date = (v) => {
    if (typeof v === "object" && v) v = v.at || v.created_at || v.completed_at;
    if (!v) return "暂无记录";
    const d = new Date(v);
    if (Number.isNaN(d.getTime())) return esc(v);
    const inShanghai = value => new Intl.DateTimeFormat("en-US", {timeZone:"Asia/Shanghai",year:"numeric"}).format(value);
    return d.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",year:inShanghai(d)===inShanghai(new Date())?undefined:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});
  };
  const empty = (title, body = "") => '<div class="empty-state"><span class="empty-symbol" aria-hidden="true">· · ·</span><b>'+esc(title)+'</b><p>'+esc(body)+'</p></div>';
  const allUsers = () => state.loaded.users ? list(state.users) : list(state.overview?.users).length ? list(state.overview.users) : list(Object.values(state.workspaces).find(w=>list(w?.users).length)?.users);
  const member = (id) => allUsers().find(u => String(u.id) === String(id));
  const memberName = (id) => (member(id)?displayName(member(id)):id) || "未标记成员";
  const selectedWork = () => state.projectId ? state.workspaces[state.projectId] || state.workspace : null;
  function workData(){
    if(state.projectId)return selectedWork();
    if(state.overview && Array.isArray(state.overview.tasks))return state.overview;
    const workspaces=Object.values(state.workspaces);
    if(!workspaces.length)return null;
    const unique=name=>[...new Map(workspaces.flatMap(w=>list(w?.[name])).map(item=>[String(item.id||item.task_id||item.feedback_id),item])).values()];
    return {tasks:unique('tasks'),task_drafts:unique('task_drafts'),deleted_task_drafts:unique('deleted_task_drafts'),feedback:unique('feedback'),proposals:unique('proposals'),deleted_proposals:unique('deleted_proposals')};
  }
  const workError = () => state.projectId ? state.errors.workspace : state.errors.overview;
  const feedbackVersion = f => String(f.id||f.feedback_id||'')+':'+String(f.content_hash||'')+':'+String(f.record_version||f.revision||'');
  const feedbackSnapshot = projectId => state.overview&&Array.isArray(state.overview.feedback)?state.overview.feedback.filter(f=>f.project_id===projectId):list(state.workspaces[projectId]?.feedback);

  // A small Markdown renderer: escape all source HTML; allow only normal links.
  function inline(v) {
    const tokens = [];
    const token = (html) => { tokens.push(html); return "\u0000"+(tokens.length-1)+"\u0000"; };
    let s = String(v).replace(/\x60([^\x60]+)\x60/g, (_, code) => token("<code>"+esc(code)+"</code>"));
    s = s.replace(/!?\[([^\]\n]+)\]\(([^)\s]+)\)/g, (all, title, url) => all.startsWith("!") ? token(esc(title)) : token(link(url, title) || esc(title)));
    s = esc(s).replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "<em>$1</em>");
    s = s.replace(/(^|[\s：:（(【])(https?:\/\/[^\s<（）()【】，。；]+)(?=[\s（）()【】，。；]|$)/g, (_, space, url) => space + (link(url.replace(/&amp;/g,"&"), url.replace(/&amp;/g,"&")) || esc(url)));
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => tokens[Number(i)]);
  }
  function markdown(v) {
    const lines = String(v ?? "").replace(/\r\n/g,"\n").split("\n"), out = [];
    let i = 0;
    const cells = (line) => line.trim().replace(/^\|/,"").replace(/\|$/,"").split("|").map(x => x.trim());
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      if (/^\s*\x60{3}/.test(line)) {
        const code = []; i++;
        while (i < lines.length && !/^\s*\x60{3}/.test(lines[i])) code.push(lines[i++]);
        i++; out.push("<pre><code>"+esc(code.join("\n"))+"</code></pre>"); continue;
      }
      const head = line.match(/^(#{1,4})\s+(.+)$/);
      if (head) { out.push("<h"+(head[1].length+1)+">"+inline(head[2])+"</h"+(head[1].length+1)+">"); i++; continue; }
      if (i+1 < lines.length && line.includes("|") && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i+1])) {
        const headers = cells(line), rows = []; i += 2;
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        out.push('<div class="table-scroll"><table><thead><tr>'+headers.map(c => "<th>"+inline(c)+"</th>").join("")+"</tr></thead><tbody>"+rows.map(r => "<tr>"+headers.map((_,j) => "<td>"+inline(r[j] || "")+"</td>").join("")+"</tr>").join("")+"</tbody></table></div>"); continue;
      }
      const bullet = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.+)$/);
      if (bullet) {
        const ordered = /^\s*\d/.test(line), items = [];
        while (i < lines.length) { const b = lines[i].match(/^\s*(?:[-*+]|\d+[.)])\s+(.+)$/); if (!b) break; items.push("<li>"+inline(b[1])+"</li>"); i++; }
        out.push("<"+(ordered?"ol":"ul")+">"+items.join("")+"</"+(ordered?"ol":"ul")+">"); continue;
      }
      if (/^>\s?/.test(line)) { const q = []; while (i < lines.length && /^>\s?/.test(lines[i])) q.push(lines[i++].replace(/^>\s?/,"")); out.push("<blockquote>"+q.map(inline).join("<br>")+"</blockquote>"); continue; }
      if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
      const p = [];
      while (i < lines.length && lines[i].trim() && (p.length === 0 || !/^(#{1,4}\s|>\s?|[-*+]\s|\d+[.)]\s|\s*\x60{3})/.test(lines[i]))) {
        if (p.length && i+1 < lines.length && lines[i].includes("|") && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i+1])) break;
        p.push(lines[i++]);
      }
      out.push("<p>"+p.map(inline).join("<br>")+"</p>");
    }
    return out.join("");
  }
  function summary(v, max = 260) {
    const s = String(v ?? "").split("\n").filter(l => l.trim() && !/^\s*(?:\[RDOS|反馈ID|成员ID|项目ID|任务ID|任务版本|运行标识|写回标识|反馈 ID|member_id|project_id|run_label)\s*[:：]/i.test(l)).slice(0,5).join("\n");
    return s.length > max ? s.slice(0,max)+"…" : s;
  }
  function evidenceContent(value) {
    if (Array.isArray(value)) return value.length ? value.map(evidenceContent).join("") : '<p class="small-note">尚未记录本轮材料依据。</p>';
    if (value && typeof value === "object") {
      const readable=value.summary||value.claim||value.description||value.content;
      return '<div class="evidence-entry">'+(readable?markdown(textValue(readable)):'<details><summary>材料记录</summary><pre>'+esc(safeJSON(value))+'</pre></details>')+link(value.source_url||value.url,"查看依据原文 ↗")+'</div>';
    }
    return markdown(textValue(value));
  }
  class APIError extends Error {
    constructor(message,status=0,code="",technical="") {super(message);this.status=status;this.code=code;this.technical=technical;}
  }
  async function api(path, options = {}) {
    const { timeout_ms=30000,text=false,...request }=options;
    const session=state.session, controller=new AbortController(), timer=setTimeout(()=>controller.abort(),timeout_ms);
    try {
      const response=await fetch("api/central"+path,{credentials:"same-origin",signal:controller.signal,...request,headers:{"Content-Type":"application/json",...(state.user?{'X-RDOS-Actor':state.user.id}:{}),...request.headers},body:request.body===undefined?undefined:JSON.stringify(request.body)});
      const raw=await response.text();let data;
      try{data=JSON.parse(raw);}catch{data=text&&response.ok?raw:null;}
      if(!response.ok){
        const d=data?.detail||data?.error||data?.message;
        const technical=typeof d==='object'?(d.technical_detail||safeJSON(d)):String(d||'');
        let message=typeof d==='object'&&!Array.isArray(d)?d.message:typeof d==='string'?d:'';
        if(response.status===401){message=path==='/auth/login'?'账号或密码不正确，请检查后重试。':'登录已过期，请重新登录。';if(state.user&&session===state.session){captureLoginDraft();resetSession(message);}}
        else if(d?.code==='session_changed'){message='当前登录账号已变化，请重新登录。';if(session===state.session)resetSession(message);}
        else if(response.status===403)message='当前账号不能执行此操作。请联系管理员核对权限。';
        else if(response.status===422&&(!d?.code||!message||!/[\u3400-\u9fff]/.test(message)))message='请检查表单中必填内容和格式，再重新提交。';
        else if(!message||!/[\u3400-\u9fff]/.test(message))message=response.status>=500?'服务暂时无法完成操作，请稍后重试。':'操作未完成，请检查当前内容后重试。';
        if(!developerMode&&/实验环境|合成|阶段\s*[0-9]|role_test|node_lab/.test(message))message='当前操作无法完成，请联系管理员检查服务状态。';
        const error=new APIError(message,response.status,d?.code||'',technical);error.projectId=d?.project_id;throw error;
      }
      if(data===null)throw new APIError('暂时无法解析检查结果，已有记录保留，可重新检查。',response.status,'parse_error');
      return data;
    }catch(e){
      if(e instanceof APIError)throw e;
      throw new APIError(e.name==='AbortError'?(request.method&&request.method!=='GET'?'操作结果尚未确认，请先检查已有结果，再继续原操作。':'读取超时，当前内容保留，请重试。'):'暂时无法连接服务，请稍后重试。',0,e.name==='AbortError'?'timeout':'connection_failed');
    }finally{clearTimeout(timer);}
  }
  function toast(message,error=false){clearTimeout(state.toastTimer);setText('toast',message);$('toast').className='toast'+(error?' error':'');state.toastTimer=setTimeout(()=>show('toast',false),error?9000:4500);}
  function showError(message){setText('workspace-error',message);show('workspace-error',!!message);}
  function errorAt(id,error){const node=$(id);if(!node)return;node.textContent=error?.message||error||'';node.classList.toggle('hidden',!node.textContent);}
  async function busy(button,action,errorId){
    if(button?.disabled)return; const c=context();
    if(button){button.disabled=true;button.setAttribute('aria-busy','true');}
    try{return await action();}catch(e){if(current(c)){if(errorId)errorAt(errorId,e);else showError(e.message);toast(e.message,true);}}
    finally{if(button?.isConnected&&sameSession(c)){button.disabled=false;button.removeAttribute('aria-busy');}}
  }
  const formValue=id=>JSON.stringify([...$(id).querySelectorAll('input,textarea,select')].map(n=>[n.id,n.type==='checkbox'?n.checked:n.multiple?[...n.selectedOptions].map(o=>o.value):n.value]));
  function rememberForm(id){state.forms.set(id,{initial:formValue(id),busy:false,context:context()});}
  function open(id){if(!$(id).open)$(id).showModal();}
  function close(id,force=false){
    const d=$(id),f=state.forms.get(id);
    if(!force){
      if(f?.busy){errorAt(f.errorId||'confirm-error','操作仍在进行。关闭界面不会取消远端操作，请等待结果。');return;}
      if(id==='credential-dialog'){errorAt('credential-close-warning','请先妥善保存初始密码，再点击“我已保存，关闭”。');return;}
      if((f&&formValue(id)!==f.initial)||(id==='proposal-dialog'&&proposalChanged())){
        confirm('放弃未保存的修改？','关闭后将放弃这次尚未保存的输入，已保存的记录保留。','放弃修改',async()=>close(id,true));return;
      }
    }
    d.close();state.forms.delete(id);
  }
  async function formAction(id,errorId,action){
    const f=state.forms.get(id)||{context:context(),initial:formValue(id)};if(f.busy)return;
    f.busy=true;f.errorId=errorId;state.forms.set(id,f);errorAt(errorId,'');
    const buttons=[...$(id).querySelectorAll('button')],disabled=buttons.map(b=>b.disabled);
    buttons.forEach(b=>{b.disabled=true;b.setAttribute('aria-busy','true');});const inputs=[...$(id).querySelectorAll('input,textarea,select')],wasDisabled=inputs.map(n=>n.disabled);inputs.forEach(n=>n.disabled=true);
    try{return await action(f.context);}catch(e){if(sameSession(f.context)){errorAt(errorId,e);if(!$(id).open)toast(e.message,true);}return null;}
    finally{f.busy=false;if(sameSession(f.context)){inputs.forEach((n,i)=>{if(n.isConnected)n.disabled=wasDisabled[i];});if(id==='project-dialog'&&state.projectPending)$(id).querySelectorAll('input,textarea').forEach(n=>n.type==='checkbox'?n.disabled=true:n.readOnly=true);if(id==='task-dialog'&&state.taskPending)lockTask(true);buttons.forEach((b,i)=>{if(b.isConnected){b.disabled=disabled[i];b.removeAttribute('aria-busy');}});if(id==='proposal-dialog'&&$(id).open)updatePublishButton();}}
  }
  function copyKey(){return [labStoragePrefix(),'rdos-copied',state.user?.id,state.onboarding?.session_id,state.onboarding?.stage,state.instruction?.id||state.instruction?.url].join(':');}
  function copiedValue(){try{return !!state.instruction&&sessionStorage.getItem(copyKey())==='yes';}catch{return false;}}
  function clearProject(resetOnboarding=false){
    phase3.sequence++;phase3.loading=false;phase3.data=null;phase3.error='';phase3.projectId=null;phase3.task=null;phase3.draft=null;phase3.action=null;phase3.taskOperation=null;
    for(const id of ['phase3-sync-status','phase3-sync-last','phase3-error'])setText(id,'');
    for(const id of ['phase3-summary','phase3-zones','phase3-tasks','phase3-drafts','phase3-documents','phase3-decisions','phase3-events'])put(id,'');
    if(state.threadId)state.chatDrafts[state.threadId]=$('chat-message').value;
    state.threadId=null;state.threads=[];state.activeRun=null;state.chatOperation=null;state.chatViewed=null;state.chatComposing=false;$('chat-message').value='';setText('chat-status','');errorAt('chat-error','');
    state.generation++;state.workspace=null;state.projectInstructionSeq++;state.projectInstruction=null;state.refreshPromise=null;
    const onboardingError=state.errors.onboarding, onboardingLoaded=state.loaded.onboarding;
    state.errors=resetOnboarding?{}:onboardingError?{onboarding:onboardingError}:{};state.loaded=resetOnboarding?{}:onboardingLoaded?{onboarding:true}:{};
    if(resetOnboarding){state.onboarding=null;state.onboardingError='';state.onboardingCheckError='';state.onboardingRequestSeq++;state.instructionError='';state.reentering=false;state.onboardingStarting=false;state.onboardingStartSeq++;state.instruction=null;state.instructionPromise=null;state.instructionSeq++;state.checking=false;state.copied=false;state.lastCheck=0;}
    if(resetOnboarding){for(const timer of state.polling.values())clearTimeout(timer);state.polling.clear();state.activeRun=null;state.selectedRun=null;state.chatOperation=null;state.chatSubmitting=false;state.syncRun=null;state.syncOperation=null;state.chatViewed=null;}
    for(const id of ['sync-feedback-status','sync-feedback-error','project-instruction-status','project-instruction-error'])errorAt(id,'');
  }
  function captureLoginDraft(){
    if(!state.user)return;
    const dialog=['project-dialog','task-dialog','proposal-dialog','user-dialog'].find(id=>$(id).open);
    const fields=dialog?[...$(dialog).querySelectorAll('input,textarea,select')].filter(n=>n.type!=='password').map(n=>({id:n.id,value:n.value,checked:n.checked})):[];
    state.loginRecovery={source:!$('source-form').classList.contains('hidden')?{url:$('source-url').value,name:$('source-name').value,watch:$('source-watch').checked,operation:state.sourceOperation}:null,userId:state.user.id,projectId:state.projectId,chat:$('chat-message').value||pendingChat()?.raw||'',chatThreadId:state.threadId,chatPending:pendingChat()?{...pendingChat()}:null,dialog,fields,
      task:state.task,taskMode:state.taskMode,proposal:state.proposal,editProject:state.editProject,editUser:state.editUser,
      taskOperation:state.taskOperation,projectOperation:state.projectOperation,publishOperation:state.publishOperation};
  }
  function restoreLoginDraft(){
    const d=state.loginRecovery;if(!d||d.userId!==state.user?.id)return;
    if(d.chatThreadId){state.chatDrafts[d.chatThreadId]=d.chat||'';state.threadSelections[d.projectId||'']=d.chatThreadId;if(d.chatPending)state.chatPending[d.chatThreadId]={...d.chatPending,status:'uncertain',error:'登录已恢复，请继续原消息以核对结果。'};}
    if(d.projectId!==state.projectId){state.loginRecovery=null;return;}
    state.loginRecovery=null;if(!d.chatThreadId||d.chatThreadId===state.threadId)$('chat-message').value=d.chat||'';renderMessages();renderConnection();
    if(d.source){$('source-url').value=d.source.url;$('source-name').value=d.source.name;$('source-watch').checked=d.source.watch;state.sourceOperation=d.source.operation;show('source-form');errorAt('source-form-error','登录已恢复，未保存的资料输入已保留。');}
    if(d.dialog==='project-dialog')showProject(d.editProject);
    if(d.dialog==='task-dialog')showTask(d.task,d.taskMode);
    if(d.dialog==='proposal-dialog'){showProposal(d.proposal);state.proposalEditing=true;show('proposal-editor');show('proposal-preview',false);}
    if(d.dialog==='user-dialog')showUser(d.editUser);
    if(d.dialog){
      const nodes=[...$(d.dialog).querySelectorAll('input,textarea,select')].filter(n=>n.type!=='password');
      d.fields.forEach((f,i)=>{const n=f.id?$(f.id):nodes[i];if(n){n.value=f.value;if(n.type==='checkbox')n.checked=f.checked;}});
      state.taskOperation=d.taskOperation;state.projectOperation=d.projectOperation;state.publishOperation=d.publishOperation;
      errorAt(d.dialog.replace('-dialog','-form-error'),'登录已恢复，未保存的输入已保留，请检查后继续。');updatePublishButton();
    }
  }
  function resetSession(message=''){
    const knowledgeDirectory=$('knowledge-list'),knowledgeTabs=$('knowledge-categories');
    if(knowledgeDirectory&&knowledgeTabs?.contains(knowledgeDirectory))knowledgeTabs.after(knowledgeDirectory);
    Object.assign(navigation,{projectId:null,collaboration:'tasks',settings:'memory',showClosed:false});
    if($('phase3-show-closed'))$('phase3-show-closed').checked=false;
    if($('thread-form'))$('thread-form').reset();
    for(const id of ['account-profile','project-navigation','phase3-events']){const node=$(id);if(node){node.replaceChildren();delete node.dataset.rendered;}}
    Object.assign(rodos,{policy:null,memories:[],preview:null,maintenance:null,runs:[],nodes:[],loading:false,settingsLoaded:false,policyDirty:false,memoryDirty:false,edit:null,tab:'agent',sequence:rodos.sequence+1,policyOp:null,memoryOp:null,maintenanceOps:{},policyConflict:null});
    for(const id of ['rodos-policy-content','rodos-memory-topic','rodos-memory-content'])if($(id))$(id).value='';
    show('rodos-memory-form',false);
    knowledge.data=null;knowledge.category='selected_rag';knowledge.categoryExpanded=true;knowledge.selectedId=null;knowledge.detail=null;knowledge.fileId=null;knowledge.fileDetail=null;knowledge.fileError='';knowledge.fileSequence++;knowledge.error='';knowledge.loading=false;knowledge.syncing=false;knowledge.sequence++;knowledge.detailSequence++;knowledge.operations={};state.users=[];
    phase3.data=null;phase3.error='';phase3.loading=false;phase3.sequence++;phase3.task=null;phase3.draft=null;phase3.action=null;phase3.taskOperation=null;phase3.syncOperation=null;
    document.querySelectorAll('[aria-busy]').forEach(b=>{b.removeAttribute('aria-busy');if(b.tagName==='BUTTON')b.disabled=false;});state.session++;clearProject(true);state.user=null;state.projectId=null;state.projects=[];state.nodes=[];state.defaultFolderUrl="";state.projectInitialized=false;state.projectActionOps={};state.syncResults={};state.phase3SyncOperations={};phase3.syncing=false;state.showInactive=false;$('show-inactive-projects').checked=false;state.sources=[];state.overview=null;state.workspaces={};state.chatDrafts={};state.chatPending={};state.chatComposing=false;state.threadSelections={};state.runItems=[];state.runNext=null;state.runTotal=0;state.page=developerMode?'work':'projects';state.forms.clear();state.task=null;state.proposal=null;state.editProject=null;state.editUser=null;
    state.sourceOperation=null;state.proposalSourceChanged=null;state.diagnostics=null;state.diagnosticsLoading=false;state.diagnosticsError='';state.diagnosticsSeq++;$('diagnostics-details').open=false;put('diagnostics-result','');setText('diagnostics-status','');errorAt('diagnostics-error','');$('source-form').reset();show('source-form',false);errorAt('source-form-error','');setText('source-form-status','');
    clearInterval(state.refreshTimer);document.querySelectorAll('dialog[open]').forEach(d=>d.close());
    for(const id of ['rodos-policy-conflict','rodos-policy-meta','rodos-policy-status','rodos-policy-error','rodos-memory-items','rodos-memory-preview','rodos-memory-status','rodos-memory-error','rodos-settings-error','rodos-schedules','rodos-active-maintenance','rodos-latest-inspection','rodos-inspection-history','people-list','knowledge-categories','knowledge-source','knowledge-list','knowledge-content','run-list','chosen-run-detail','source-list','runtime-state','project-health-list','project-list','thread-select','active-run','chat-status','chat-error','task-list','task-draft-list','feedback-list','proposal-list','diagnostics-result','diagnostics-status','diagnostics-error','onboarding-check-report','onboarding-prompt','project-prompt','project-instruction-link','phase3-history','proposal-review','phase3-action-preview','phase3-task-input','phase3-task-editable','phase3-task-node','project-members','task-members','task-project','task-current-project','password-target','confirm-message','confirm-error','chat-project','phase3-project']){const node=$(id);if(node){node.replaceChildren();delete node.dataset.rendered;}}for(const id of ['user-form','project-form','task-form','phase3-task-form','phase3-action-form','password-form']){$(id).reset();$(id).querySelectorAll('input,textarea,select').forEach(node=>node.disabled=false);}$('credential-username').value='';clearTimeout(state.toastTimer);setText('toast','');show('toast',false);
    show('app-view',false);show('login-view');$('messages').replaceChildren();delete $('messages').dataset.rendered;$('chat-message').value='';$('credential-password').value='';$('user-password').value='';$('password-form').reset();if($('account-menu'))$('account-menu').open=false;setText('login-error',message);
  }
  function beginSession(user){resetSession();if(state.loginRecovery&&state.loginRecovery.userId!==user.id)state.loginRecovery=null;state.user=user;state.projectId=developerMode?(labInfo?.project_id||null):null;show('login-view',false);show('app-view');render();state.refreshTimer=setInterval(()=>{if(!document.hidden&&state.user){refresh();maybeCheck();}},30000);}
  async function refresh(){
    if(!state.user)return {ok:false};if(state.refreshPromise)return state.refreshPromise;
    const c=context(),seq=++state.refreshSeq,onboardingSeq=state.checking||state.onboardingStarting?null:++state.onboardingRequestSeq;
    const work=async()=>{
      let projects,projectMetadata;
      try{const data=await api('/projects'+(developerMode?'?developer=true':'?include_inactive=true'));projects=list(data.items);projectMetadata=data;}catch(e){if(!current(c))return {ok:false};state.errors.projects=e.message;}
      if(!current(c)||seq!==state.refreshSeq)return {ok:false};
      if(projects){state.projects=projects;state.nodes=list(projectMetadata.nodes);state.defaultFolderUrl=projectMetadata.default_folder_url||'';state.loaded.projects=true;delete state.errors.projects;
        if(!developerMode&&!state.projectInitialized){state.projectInitialized=true;const first=projects.find(p=>p.is_default)||projects.find(p=>p.status==='active');if(first){clearProject();state.projectId=first.id;c.generation=state.generation;c.projectId=first.id;c.threadId=null;}}
        if(state.projectId&&!projects.some(p=>p.id===state.projectId)){
          clearProject();state.projectId=null;state.selectionNotice='原项目已不可访问，现显示全部项目。';
          c.generation=state.generation;c.projectId=null;
        }
      }
      if(admin()){
        try{
          const response=await api('/threads?project_id='+encodeURIComponent(state.projectId||''));
          if(!current(c)||seq!==state.refreshSeq)return {ok:false};
          state.threads=list(response.items);
          const desired=state.threadId||state.threadSelections[state.projectId||''];
          const selected=state.threads.find(t=>t.id===desired)||state.threads[0];
          state.threadId=selected?.id||null;c.threadId=state.threadId;
          if(state.threadId)state.threadSelections[state.projectId||'']=state.threadId;
          delete state.errors.threads;
        }catch(e){if(current(c))state.errors.threads=e.message;}
      }
      renderNavigation();
      const threadQuery=state.threadId?'&thread_id='+encodeURIComponent(state.threadId):'';
      const onboardingStartSeq=state.onboardingStartSeq;
      const requests=[state.projectId&&admin()?api('/workspace?project_id='+encodeURIComponent(state.projectId)+threadQuery):Promise.resolve(null),Promise.resolve(null),developerMode&&admin()?api('/sources'):Promise.resolve({items:[]}),admin()?api('/overview'+(state.threadId?'?thread_id='+encodeURIComponent(state.threadId):'')):Promise.resolve(null),admin()?api('/users'):Promise.resolve(null)];
      const results=await Promise.allSettled(requests);
      if(!current(c)||seq!==state.refreshSeq)return {ok:false};
      if(results[0].status==='fulfilled'&&results[0].value?.user?.id&&results[0].value.user.id!==state.user.id){resetSession('当前登录账号已变化，请重新登录。');return {ok:false};}
      ['workspace','onboarding','sources','overview','users'].forEach((name,i)=>{
        if(name==='onboarding'&&(onboardingSeq===null||onboardingSeq!==state.onboardingRequestSeq||state.checking||state.onboardingStarting||state.onboardingStartSeq!==onboardingStartSeq))return;
        const r=results[i];if(r.status==='rejected'){state.errors[name]=r.reason.message;if(name==='onboarding')state.onboardingError=r.reason.message;return;}
        delete state.errors[name];state.loaded[name]=true;
        if(name==='workspace'){state.workspace=r.value;if(r.value&&state.projectId)state.workspaces[state.projectId]=r.value;if(r.value?.user)state.user=r.value.user;}
        if(name==='onboarding'){state.onboardingError='';if(r.value)acceptOnboarding(r.value);}
        if(name==='sources')state.sources=list(r.value.items);
        if(name==='overview'&&r.value)state.overview=r.value;
        if(name==='users'&&r.value)state.users=list(r.value.items);
      });
      if(admin()&&!running(state.activeRun)){
        const threadRuns=[...list(state.overview?.runs),...list(state.workspace?.runs)]
          .filter(run=>run.kind==='chat'&&run.trigger==='manual'&&run.thread_id===state.threadId&&String(run.project_id||'')===String(state.projectId||''));
        state.activeRun=threadRuns.sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')))[0]||null;
      }
      render();
      if(admin())for(const run of [...list(state.overview?.runs),...list(state.workspace?.runs)])if(running(run)){if(run.thread_id===state.threadId&&String(run.project_id||'')===String(state.projectId||'')&&run.kind!=='sync'&&run.trigger!=='scheduled'&&!running(state.activeRun))state.activeRun=run;trackRun(run.id);}
      renderConnection();restoreLoginDraft();const names={projects:'项目列表',workspace:'项目内容',onboarding:'接入状态',sources:'共享资料',overview:'团队概览',users:'成员账号'};
      if(state.page==='collaboration')await loadPhase3();
      if(state.page==='knowledge')await loadKnowledge();
      if(admin()&&['global','agent'].includes(state.page))await loadRodos();
      const failures=Object.keys(state.errors).map(k=>names[k]||k);return {ok:!failures.length,failures};
    };
    const pending=work();state.refreshPromise=pending;
    try{return await pending;}catch(e){if(current(c)){showError(e.message);return {ok:false,failures:['当前页面']};}}finally{if(state.refreshPromise===pending)state.refreshPromise=null;}
  }
  const onboardingKey=data=>JSON.stringify([data?.session_id,data?.choice,data?.stage,data?.stage_status,list(data?.completed_steps),list(data?.missing),data?.ready_for_work]);
  const continuationKey=data=>[labStoragePrefix(),'rdos-onboarding-continued',state.user?.id,data?.session_id,data?.stage].join(':');
  const continued=data=>{try{return sessionStorage.getItem(continuationKey(data))==='yes';}catch{return false;}};
  const nextOnboardingStep=data=>data&&!data.ready_for_work&&data.stage_status==='pending'&&list(data.completed_steps).length&&[1,3].includes(Number(data.stage))&&!continued(data)?Number(data.stage):null;
  function acceptOnboarding(data){
    if(state.onboarding&&onboardingKey(state.onboarding)!==onboardingKey(data)){state.instruction=null;state.instructionSeq++;state.instructionPromise=null;state.copied=false;}
    state.onboarding=data;
    if(data.choice&&!data.ready_for_work&&!nextOnboardingStep(data)&&!data.error&&!(data.stage_status==='blocked'&&!list(data.missing).length)&&!state.instruction&&!state.instructionPromise){const c=onboardingContext();prepareInstruction(false).catch(e=>{if(currentOnboarding(c)){state.instructionError=e.message;renderOnboarding();}});}
  }
  const pageInfo={onboard:['Agent 接入',''],projects:['项目管理',''],agent:['中央Agent',''],collaboration:['协作与任务',''],work:['测试工作',''],global:['设置',''],knowledge:['共享知识管理',''],account:['我的账号','']};
  function renderSidebar(){
    document.querySelectorAll('[data-sidebar-page]').forEach(el=>el.classList.toggle('hidden',el.dataset.sidebarPage!==state.page));
    show('workspace-sidebar',!developerMode||state.page!=='work');
    document.querySelectorAll('[data-collaboration-view]').forEach(el=>el.classList.toggle('hidden',el.dataset.collaborationView!==navigation.collaboration));
    document.querySelectorAll('[data-settings-view]').forEach(el=>el.classList.toggle('hidden',el.dataset.settingsView!==navigation.settings));
    for(const [attribute,value] of [['collaboration-view-select',navigation.collaboration],['settings-view-select',navigation.settings]]){
      document.querySelectorAll('[data-'+attribute+']').forEach(el=>{const active=el.getAttribute('data-'+attribute)===value;el.classList.toggle('active',active);el.setAttribute('aria-current',active?'page':'false');});
    }
    document.querySelectorAll('[data-project-view-select]').forEach(el=>{const active=!navigation.projectId;el.classList.toggle('active',active);el.setAttribute('aria-current',active?'page':'false');});
    document.querySelectorAll('[data-project-view-id]').forEach(el=>{const active=el.dataset.projectViewId===navigation.projectId;el.classList.toggle('active',active);el.setAttribute('aria-current',active?'page':'false');});
  }
  function renderNavigation(){
    if(!['projects','agent','work','global','collaboration','knowledge','account'].includes(state.page)||(!developerMode&&state.page==='work'))state.page='projects';
    if(!admin()&&['agent','work','global'].includes(state.page))state.page='projects';
    document.querySelectorAll('.admin-only:not(.main-page)').forEach(el=>el.classList.toggle('hidden',!admin()));
    document.querySelectorAll('.page-tab').forEach(el=>{const yes=el.dataset.page===state.page;el.classList.toggle('active',yes);el.setAttribute('aria-current',yes?'page':'false');});
    document.querySelectorAll('.main-page').forEach(el=>el.classList.toggle('hidden',el.id!=='page-'+state.page||(el.classList.contains('admin-only')&&!admin())));
    setText('page-title',pageInfo[state.page][0]);setText('page-description',pageInfo[state.page][1]);
    show('new-project',admin()&&state.page==='projects');
    show('new-thread',admin()&&state.page==='agent');
    show('phase3-sync',admin()&&state.page==='collaboration');
    show('phase3-new',admin()&&state.page==='collaboration');
    show('knowledge-heading-controls',state.page==='knowledge');
    renderSidebar();
  }
  function render(){
    if(!state.user)return;
    put('current-user','<span>'+esc(displayName(state.user))+'</span>'+list(state.user.roles).map(r=>pill(r)).join('')+(developerMode&&state.user.identity_mode==='role_test'?pill('角色测试','warn'):''));
    renderNavigation();
    const names={projects:'项目列表',workspace:'项目内容',onboarding:'接入状态',sources:'共享资料',overview:'团队概览',users:'成员账号'};
    const failures=Object.entries(state.errors).filter(([k])=>state.page!=='onboard'&&k!=='onboarding'&&!(k==='workspace'&&state.page==='agent')).map(([k,v])=>(names[k]||k)+'：'+v);
    showError(failures.length?'更新失败，'+(state.onboarding||state.workspace||state.loaded.projects?'当前显示上次内容。':'请重试。')+' '+failures.join(' '):state.page==='onboard'?'':state.selectionNotice||'');
    renderProjects();renderPhase3();renderKnowledge();
    put('account-profile','<h2>'+esc(displayName(state.user))+'</h2><dl class="phase3-meta"><dt>登录名</dt><dd>'+esc(state.user.username||'尚未设置')+'</dd><dt>角色</dt><dd>'+list(state.user.roles).map(role=>esc(label(role))).join('、')+'</dd></dl>');
    if(admin()){renderConnection();renderMessages();renderPeople();renderRuntime();if(developerMode){renderWorkScope();renderTasks();renderTaskDrafts();renderFeedback();renderProposals();renderDiagnostics();renderSources();renderRuns();}}
    renderSidebar();
  }
  function renderThreads(){
    put('chat-project','<option value="">机构讨论</option>'+state.projects.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+(p.status==='inactive'?'（已停用）':'')+'</option>').join(''));
    $('chat-project').value=state.projectId||'';
    put('thread-select',state.threads.length?state.threads.map(t=>'<option value="'+esc(t.id)+'">'+esc(t.title||'新对话')+(t.readonly?' · 历史只读':'')+' · '+date(t.created_at)+'</option>').join(''):'<option value="">尚无对话，请点击“新对话”</option>');
    $('thread-select').value=state.threadId||'';
    $('thread-select').disabled=!state.threads.length;
    const thread=state.threads.find(t=>t.id===state.threadId);
    setText('thread-status',thread?.readonly?'历史对话仅供查看；请新建对话继续。':'');
  }
  function sizeChatComposer(){
    const field=$('chat-message');
    field.style.height='auto';
    field.style.height=Math.min(Math.max(field.scrollHeight,44),152)+'px';
  }
  const pendingChat = () => {
    const pending=state.chatPending[state.threadId];
    return pending&&pending.projectId===(state.projectId||'')?pending:null;
  };
  const currentChatRun = () => state.activeRun?.thread_id===state.threadId&&String(state.activeRun.project_id||'')===String(state.projectId||'')?state.activeRun:null;
  function renderConnection(){
    renderThreads();
    const thread=state.threads.find(t=>t.id===state.threadId),pending=pendingChat();
    const blocked=!admin()||!thread||thread.readonly||['submitting','running'].includes(pending?.status)||(running(currentChatRun())&&!pending?.error);
    setText('agent-status','中央模型 '+(labInfo?.runtime?.model_mode||'mock')+'；实际调用结果请查看运行记录。');
    $('chat-send').disabled=blocked;$('chat-message').disabled=!admin()||!thread||thread.readonly;
    $('new-thread').disabled=false;
    show('connection-banner',false);
  }
  function chatStage(run){
    const step=list(run?.steps).at(-1);
    if(!step||step.stage==='request_interpretation'||step.type==='model_request'||step.type==='model_response')return '思考中…';
    if(step.type==='tool_call'||step.type==='tool_result'){
      const name=String(step.name||'');
      if(name==='phase3_execute_task_request')return step.type==='tool_result'?'正在整理办理结果…':'正在办理任务并核对结果…';
      if(/memory/.test(name))return '正在查找相关约定…';
      if(name==='central_refresh_feishu')return '正在启动飞书刷新…';
      if(/inspect|diagnos/.test(name))return '正在核对相关记录…';
      if(/knowledge|source|document|workspace|list_|read_|search/.test(name))return '正在读取相关资料…';
      return '正在核对结果…';
    }
    return '思考中…';
  }
  function chatMessageCard(message){
    const isUser=message.role==='user',name=isUser?(message.user_id&&message.user_id!==state.user?.id?message.speaker_name||'成员':'你'):'Rodos_中央Agent';
    return '<article class="message '+(isUser?'user':'agent')+'" data-key="'+esc(message.id||message.run_id+':'+message.role)+'"><span class="message-name">'+esc(name)+'</span><div class="message-body markdown-content">'+markdown(textValue(message.content))+'</div>'+(developerMode&&message.run_id?'<button class="quiet" type="button" data-run="'+esc(message.run_id)+'">查看执行详情</button>':'')+'</article>';
  }
  function runFailure(run){
    const steps=list(run?.steps),interpretationTimedOut=run?.error_code==='agent_timeout'&&
      steps.some(step=>step.type==='retry'&&step.stage==='request_interpretation'&&step.code==='model_timeout')&&
      !steps.some(step=>step.type==='tool_call');
    return interpretationTimedOut?'等待中央模型理解请求超时；本次尚未办理任务。可以稍后重试原请求。':readableError(run?.error||'这次处理未完成，已保留你的消息。');
  }
  function renderMessages(){
    if(state.page!=='agent')return;
    const firstView=state.chatViewed!==state.threadId;
    const box=$('messages'),atEnd=box.scrollHeight-box.scrollTop-box.clientHeight<70;
    const data=state.projectId?state.workspace:state.overview;
    const messages=list(data?.messages).filter(m=>state.threadId&&m.thread_id===state.threadId&&m.trigger!=='scheduled'&&m.kind!=='sync').sort((a,b)=>String(a.created_at||'').localeCompare(String(b.created_at||'')));
    const pending=pendingChat(),run=currentChatRun();
    const persisted=pending&&messages.some(m=>m.role==='user'&&(m.operation_id===pending.id||(pending.runId&&m.run_id===pending.runId)));
    if(pending&&persisted&&pending.status==='finished')delete state.chatPending[state.threadId];
    const local=pending&&!persisted?[{id:'pending:'+pending.id,role:'user',content:pending.raw,created_at:pending.created_at}]:[];
    const content=[...messages,...local].sort((a,b)=>String(a.created_at||'').localeCompare(String(b.created_at||''))).map(chatMessageCard).join('');
    const active=pending&&['submitting','running'].includes(pending.status)||running(run)&&!pending?.error;
    const answered=run&&messages.some(m=>m.role!=='user'&&m.run_id===run.id);
    const failure=pending?.error||run&&!running(run)&&!['succeeded','completed'].includes(run.status)&&!answered&&runFailure(run);
    const recovery=pending?.error?'<button class="secondary chat-retry" type="button" data-chat-retry="'+esc(pending.id)+'">继续这条消息</button>':run&&['failed','interrupted','needs_attention','uncertain'].includes(run.status)?'<button class="secondary" type="button" data-retry-run="'+esc(run.id)+'">继续这次处理</button>':'';
    const progress=active?'<article class="message agent thinking" data-key="chat-progress" role="status"><span class="message-name">Rodos_中央Agent</span><div class="chat-progress"><span class="activity-dot" aria-hidden="true"></span><span>'+esc(pending?.status==='submitting'?'思考中…':chatStage(run))+'</span></div></article>':failure?'<article class="message agent chat-failure" data-key="chat-failure"><span class="message-name">Rodos_中央Agent</span><p>'+esc(failure)+'</p>'+recovery+'</article>':'';
    put('messages',!state.threadId?empty('尚未选择对话','点击“新对话”后开始讨论。'):content||progress?content+progress:!data?empty('对话暂时无法读取','请刷新重试。'):empty('本对话尚无消息','可以从项目资料或当前工作开始讨论。')+'<div class="chat-examples">'+['整理本项目资料','准备一份任务草稿'].map(t=>'<button class="secondary" type="button" data-chat-example="'+esc(t)+'">'+t+'</button>').join('')+'</div>');
    if(firstView){$('chat-message').value=state.chatDrafts[state.threadId]||'';state.chatViewed=state.threadId;box.scrollTop=box.scrollHeight;}
    else if(atEnd)box.scrollTop=box.scrollHeight;
    sizeChatComposer();
    renderActiveRun();
  }
  function renderActiveRun(){
    const run=currentChatRun();
    show('active-run',developerMode&&!!run&&!running(run)&&!['succeeded','completed'].includes(run.status));
    if(developerMode&&run&&!running(run))put('active-run',runContent(run,true));
  }
  function stepSources(step) {
    let r=step.result; if (typeof r==="string") { try { r=JSON.parse(r); } catch { r=null; } }
    const known=[...list(state.overview?.sources),...list(state.workspace?.sources)].find(s => s.id===(step.arguments?.object_id||step.arguments?.id));
    return [...new Set([step.source_url,r?.source_url,known?.source_url,...list(r?.items).map(x=>x.source_url),...list(r?.sources).map(x=>x.source_url||x.url)].map(safeUrl).filter(Boolean))].slice(0,8).map(u=>link(u,"打开实际来源 ↗")).join(" · ");
  }
  function unavailable(){return empty(workError()?'内容暂时无法读取':'正在加载',workError()?'请稍后重新打开此页，已有内容保留。':'');}
  function renderWorkScope(){
    const scoped=!!state.projectId;setText('work-scope-label',scoped?'当前项目：'+projectName(state.projectId):'全部项目');show('work-show-all',scoped);
    show('sync-feedback',scoped);$('sync-feedback').disabled=!scoped||!!state.syncRun;
    if(!scoped){setText('sync-feedback-status','');errorAt('sync-feedback-error','');}
  }
  function taskTitle(t){
    if(t.title)return t.title;
    if(t.related_task){
      const people=list(t.member_ids).map(memberName).join('、')||'成员';
      return people+'的'+(t.task_kind==='onboarding'?'接入':'工作')+'补充要求';
    }
    const clean=String(t.instructions||'').replace(/[#*`]/g,'').replace(/^(?:一次性任务|补充要求)[（(][^）)]+[）)][。.]?\s*/,'').replace(/\b(?:mem|run|rec)_[A-Za-z0-9_-]+\b/g,'').replace(/\s+/g,' ').trim();
    const first=clean.split(/[。！？\n]/)[0]||'未命名任务';return first.length>64?first.slice(0,64)+'…':first;
  }
  function renderTasks(){
    const data=workData(),tasks=list(data?.tasks);setText('task-count',tasks.filter(t=>!withdrawn(t)).length+' 项有效');
    put('task-list',!data?unavailable():tasks.length?tasks.map(t=>'<article class="item-card '+(withdrawn(t)?'withdrawn':'')+'" data-key="task:'+esc(t.task_id)+'"><div class="row"><h3>'+esc(taskTitle(t))+'</h3>'+(withdrawn(t)?pill('已撤回'):['已发布','published','active'].includes(t.status)?'':pill(t.status))+'</div><div class="item-meta">'+(!state.projectId?'<span>'+esc(projectName(t.project_id))+'</span>':'')+'<span>适用：'+esc(list(t.member_ids).map(memberName).join('、')||'组织共享接入要求')+'</span>'+(t.updated_at||t.created_at?'<span>'+date(t.updated_at||t.created_at)+'</span>':'')+'</div><details><summary>查看任务要求</summary><div class="markdown-content">'+markdown(t.instructions)+'</div><p>版本 '+esc(t.version||1)+(t.task_kind==='onboarding'?' · '+esc(t.stage):'')+' · '+esc(t.nature||'一次性')+'</p><details><summary>任务标识与关联</summary><p class="mono">'+esc(t.task_id)+(t.related_task?' · '+esc(t.related_task):'')+'</p></details></details><div class="item-actions">'+link(t.source_url,'查看测试任务来源')+(!withdrawn(t)&&t.project_id?'<button class="quiet" data-edit-task="'+esc(t.task_id)+'" type="button">编辑任务</button><button class="quiet danger" data-withdraw-task="'+esc(t.task_id)+'" type="button">撤回任务</button>':'')+'</div></article>').join(''):empty('暂无已发布任务','可以新建任务，或请中央 Agent 起草。'));
  }
  function draftControls(kind,item){
    if(!admin()||item.status==='published'||item.publication_operation_id)return '';
    return '<button class="quiet'+(item.status==='deleted'?'':' danger')+'" data-draft-kind="'+esc(kind)+'" data-draft-lifecycle="'+(item.status==='deleted'?'restore':'delete')+'" data-draft-id="'+esc(item.id)+'" type="button">'+(item.status==='deleted'?'恢复草稿':'删除草稿')+'</button>';
  }
  function deletedDrafts(kind,items){
    const values=[...new Map(list(items).filter(item=>item.status==='deleted').map(item=>[item.id,item])).values()];
    return values.length?'<details class="deleted-drafts"><summary>已删除草稿（'+values.length+'）</summary>'+values.map(item=>'<article class="item-card"><h3>'+esc(item.payload?.title||item.title||item.instructions||'草稿')+'</h3>'+draftControls(kind,item)+'</article>').join('')+'</details>':'';
  }
  async function changeDraftLifecycle(kind,id,action,button){
    const data=workData(),pool=kind==='phase3'?[...phase3Drafts(),...list(phase3.data?.deleted_drafts)]:kind==='task-drafts'?[...list(data?.task_drafts),...list(data?.deleted_task_drafts)]:[...list(data?.proposals),...list(data?.deleted_proposals)];
    const item=pool.find(item=>item.id===id);if(!item||!admin())return;
    const c=context(),operationId=uid();confirm(action==='delete'?'删除这份草稿？':'恢复这份草稿？',action==='delete'?'草稿会移入已删除列表，历史保留；此前对话确认将失效。':'恢复后需要重新确认，旧授权不会恢复。',action==='delete'?'删除草稿':'恢复草稿',async()=>{
      await api((kind==='phase3'?'/phase3/drafts/':'/'+kind+'/')+encodeURIComponent(id)+'/'+action+(kind==='phase3'?phase3Query():''),{method:'POST',body:{operation_id:operationId,expected_hash:item.draft_hash},timeout_ms:180000});
      if(!current(c))return;if(kind==='phase3')await loadPhase3();else await refresh();toast(action==='delete'?'草稿已删除，可以恢复。':'草稿已恢复，请重新确认。');
    });
  }
  function renderTaskDrafts(){
    const data=workData(),items=list(data?.task_drafts).filter(item=>item.status!=='deleted');setText('draft-count',items.filter(x=>x.status==='draft').length+' 待审阅');
    put('task-draft-list',!data?unavailable():items.length?items.map(t=>'<article class="item-card" data-key="draft:'+esc(t.id)+'"><div class="row"><h3>'+esc(taskTitle(t))+'</h3>'+pill(t.publication_operation_id&&t.status!=='published'?'发布待核对':t.status==='draft'?'待审阅':t.status,t.status==='draft'?'warn':'')+'</div><p>'+esc(draftActionLabel(t))+' · '+(!state.projectId?esc(projectName(t.project_id))+' · ':'')+'适用：'+esc(list(t.member_ids).map(memberName).join('、')||'待确认')+'</p>'+(t.reason?'<p>理由：'+esc(t.reason)+'</p>':'')+(t.rejection_reason?'<p>退回说明：'+esc(t.rejection_reason)+'</p>':'')+'<details><summary>查看原任务与版本</summary><p>'+esc(t.task_id||'新任务')+(t.base_task_version?' · 原版本 '+esc(t.base_task_version):'')+'</p></details><div class="item-actions">'+(t.status!=='published'?'<button class="secondary" data-task-draft="'+esc(t.id)+'" type="button">'+(t.publication_operation_id?'检查发布结果':'审阅任务草稿')+'</button>'+(!t.publication_operation_id?'<button class="quiet danger" data-reject-task="'+esc(t.id)+'" type="button">退回修改</button>':''):link(t.source_url,'查看已发布任务'))+draftControls('task-drafts',t)+'</div></article>').join(''):empty('暂无任务草稿','可请中央 Agent 起草任务。'));
    put('task-draft-list',$('task-draft-list').innerHTML+deletedDrafts('task-drafts',[...list(data?.task_drafts),...list(data?.deleted_task_drafts)]));
  }
  function draftActionLabel(t){return ({create:'新建任务',update:'修改任务',withdraw:'停用任务'})[t?.draft_action]||'任务草稿';}
  const feedbackSectionLabels={progress:'当前进度',outputs:'本期成果',blockers:'需要协作',human_gate:'评审与确认线索',skill_rag:'Skill／RAG 使用',local_skill:'本地 Skill 更新'};
  const feedbackVersionStatus=v=>['stale','旧版本反馈'].includes(v)?'旧版本反馈':['current','当前版本'].includes(v)?'当前版本':['mixed','部分旧版本'].includes(v)?'部分任务版本需核对':'版本待核对';
  function renderFeedback(){
    const data=workData(),items=list(data?.feedback);setText('feedback-count',items.length+' 条');
    put('feedback-list',!data?unavailable():items.length?items.map(f=>{
      const taskRefs=list(f.task_refs),associated=!!f.task_id||taskRefs.length>0;
      const readback=f.source_readback===true||(f.source_readback==null&&!!f.source_url&&!!f.content_hash&&String(f.evidence_status||'').includes('正文已从飞书读回'));
      const evidence=readback?'合成反馈已读回':f.evidence_status==='self_reported'?'Agent 报告':'尚未读回';
      const version=feedbackVersionStatus(f.version_status);
      const sections=f.sections&&typeof f.sections==='object'?f.sections:{};
      const highlighted=['progress','outputs','blockers','human_gate'].filter(k=>String(sections[k]||'').trim());
      const more=['skill_rag','local_skill'].filter(k=>String(sections[k]||'').trim());
      const section=k=>'<section class="feedback-fact"><h4>'+feedbackSectionLabels[k]+'</h4><div class="markdown-content">'+markdown(sections[k])+'</div></section>';
      const reportTime=f.reported_at||f.report_time||f.feedback_time;
      const reportLabel=reportTime?'反馈时间：'+date(reportTime):'反馈时间未注明';
      const metadata='<div class="item-meta">'+(!state.projectId?'<span>'+esc(projectName(f.project_id))+'</span>':'')+'<span>'+(associated?'任务反馈':'项目反馈')+'</span><span>'+esc(reportLabel)+'</span>'+(f.period?'<span>周期：'+esc(f.period)+'</span>':'')+(member(f.member_id)?.identity_mode==='role_test'||f.identity_mode==='role_test'?pill('角色测试','warn'):'')+(associated&&version!=='当前版本'?pill(version,version==='旧版本反馈'?'warn':''):'')+'</div>';
      const facts=highlighted.length?'<div class="feedback-facts">'+highlighted.map(section).join('')+'</div>':'<div class="markdown-content">'+markdown(f.summary||summary(textValue(f.content),180))+'</div>';
      const optional=more.length?'<details><summary>Skill 与方法使用</summary><div class="feedback-facts">'+more.map(section).join('')+'</div></details>':'';
      const refs=taskRefs.length?taskRefs.map(r=>esc(r.task_id||'未标记任务')+(r.task_version?' · v'+esc(r.task_version):' · 版本未标明')+(feedbackVersionStatus(r.version_status)==='旧版本反馈'?' · 旧版本':'')).join('；'):f.task_id?esc(f.task_id)+(f.task_version?' · v'+esc(f.task_version):' · 版本未标明'):'';
      const detail='<details><summary>查看完整反馈与依据</summary><div class="markdown-content">'+markdown(textValue(f.content))+'</div>'+(f.observed_at?'<p class="small-note">系统读取：'+date(f.observed_at)+'</p>':'')+'<p class="small-note">'+(readback?'本地合成正文已读回；真实客户端读取、执行及反馈均待验证。':'原文尚未读回，请先更新反馈。')+(version==='旧版本反馈'?'旧版本反馈仍可参考。':'')+'</p>'+(associated?'<p>关联任务：'+refs+'</p>':'')+(f.correction_of?'<p>更正原反馈：'+esc(f.correction_of)+'</p>':'')+(f.template_version?'<p>反馈说明版本：'+esc(f.template_version)+'</p>':'')+'<details><summary>记录标识</summary><pre>'+esc(safeJSON({id:f.id,run_label:f.run_label,evidence_status:f.evidence_status,...(associated?{version_status:f.version_status}:{})}))+'</pre></details></details>';
      return '<article class="item-card" data-key="feedback:'+esc(f.id||f.feedback_id)+'"><div class="row"><h3>'+esc(memberName(f.member_id))+'</h3>'+pill(evidence,readback?'good':'')+'</div>'+metadata+facts+optional+detail+'<div class="item-actions">'+link(f.source_url,'查看反馈原文')+'</div></article>';
    }).join(''):empty('暂无相关反馈',state.projectId?'点击“检查一次”读取本地测试资料。':'进入项目后可检查本地测试资料。'));
  }
  const templateNames={'onboarding.0':'连接飞书指令','onboarding.1':'工作范围指令','onboarding.3':'定时同步指令','project.instructions':'项目参与指令','project.feedback':'项目反馈说明','project.periodic':'周期反馈说明'};
  function proposalTarget(p){return p.target_name||(p.target_kind==='template'?templateNames[p.target_id]||'工作指令模板':null)||list(state.sources).find(s=>s.id===p.target_id||s.object_id===p.target_id||s.token===p.target_id||s.source_url===p.source_url||s.url===p.source_url)?.name||({docx:'飞书文档',doc:'飞书文档',onboarding:'接入指引',RAG:'共享知识资料',Skill:'共享 Skill'})[p.target_kind]||'方法资料';}
  function proposalImpact(p){
    if(p.target_kind!=='template')return '影响使用此资料的项目和成员。';
    if(String(p.target_id||'').startsWith('onboarding.'))return '确认后用于所有成员新生成的接入指令；读取和采用情况需后续核对。';
    return '确认后用于所有项目新生成的工作指令；读取和采用情况需后续核对。';
  }
  function proposalTargetDisplay(p){return p.target_kind==='template'?esc(proposalTarget(p))+'（本地模板）':link(p.source_url,proposalTarget(p))||esc(proposalTarget(p));}
  function renderProposals(){
    const data=workData(),items=list(data?.proposals).filter(item=>item.status!=='deleted');setText('proposal-count',items.filter(p=>p.status==='draft').length+' 待审阅');
    put('proposal-list',!data?unavailable():items.length?items.map(p=>'<article class="item-card" data-key="proposal:'+esc(p.id)+'"><div class="row"><h3>'+esc(p.title||proposalTarget(p))+'</h3>'+pill(p.publication_operation_id&&p.status!=='published'?'发布待核对':p.status==='draft'?'待审阅':p.target_kind==='template'&&p.status==='published'?'已生效':p.status,p.status==='draft'?'warn':'')+'</div><p>'+(!state.projectId?'<span class="small-note">'+(p.target_kind==='template'?'组织共用指令':esc(projectName(p.project_id)))+' · </span>':'')+proposalTargetDisplay(p)+'</p><div class="markdown-content">'+markdown(summary(p.summary,200))+'</div><details><summary>查看修改依据</summary>'+evidenceContent(p.evidence)+'</details>'+(p.rejection_reason?'<p>退回说明：'+esc(p.rejection_reason)+'</p>':'')+(p.status==='published'?'<p>'+(p.target_kind==='template'?'后续使用：':'采用效果：')+esc(p.adoption_status||'尚待后续反馈核对')+'</p>':'')+'<div class="item-actions"><button class="secondary" data-proposal="'+esc(p.id)+'" type="button">'+(p.status==='published'?'查看修改':'审阅草稿')+'</button>'+draftControls('proposals',p)+'</div></article>').join(''):empty('暂无方法建议','可与中央 Agent 讨论改进。'));
    put('proposal-list',$('proposal-list').innerHTML+deletedDrafts('proposals',[...list(data?.proposals),...list(data?.deleted_proposals)]));
  }
  function memberNodes(user){
    const linked=[...list(user.nodes),...registeredNodes().filter(node=>node.member_id===user.id)];
    return [...new Map(linked.map(node=>[phase3NodeId(node),node])).values()].filter(node=>phase3NodeId(node));
  }
  const knowledgeCategories=[['selected_rag','Selected RAG'],['shared_skills','Shared Skills']];
  const knowledgeItems=()=>list(knowledge.data?.items).filter(item=>item.category===knowledge.category&&item.available!==false);
  const knowledgeStatus=item=>item.available===false?'来源不可用':item.content_status==='skill_package'?(item.sync_status==='failed'?'同步未完成，保留原版':'Skill 已同步'):({full_text:'正文已同步',attachment_only:'附件 · 正文未解析',file_listing:'包文件 · 未执行',folder:'文件夹'})[item.content_status]||'正文未同步';
  const knowledgeVersion=item=>item.content_status==='skill_package'&&item.version?'包版本 '+String(item.version).split(':').pop().slice(0,10):item.source_revision?'源版本 '+item.source_revision:item.version?.startsWith('sha256:')?'快照 '+item.version.slice(7,17):item.version?'版本 '+String(item.version).slice(0,30):'';
  function knowledgeLink(url,title='打开飞书原文 ↗'){
    const registered=[...list(knowledge.data?.sources),...list(knowledge.data?.items),...list(knowledge.detail?.files)].some(item=>item.source_url===url);
    if(!registered)return '';
    try{const parsed=new URL(url);if(parsed.protocol!=='https:'||parsed.hostname!=='scn33386s7ui.feishu.cn'||parsed.username||parsed.password||parsed.port||!new RegExp('^/(?:docx|file|drive/folder)/').test(parsed.pathname))return '';}catch{return '';}
    return '<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">'+esc(title)+'</a>';
  }
  async function loadKnowledge(){
    if(!state.user||knowledge.loading)return;const c={session:state.session},seq=++knowledge.sequence;knowledge.loading=true;renderKnowledge();
    try{const data=await api('/knowledge');if(!sameSession(c)||seq!==knowledge.sequence)return;
      const previous=list(knowledge.data?.items).find(item=>item.id===knowledge.selectedId);
      knowledge.data=data;knowledge.error='';
      const next=list(data.items).find(item=>item.id===knowledge.selectedId&&item.category===knowledge.category);
      if(!next){knowledge.selectedId=null;knowledge.detail=null;knowledge.fileId=null;knowledge.fileDetail=null;knowledge.detailSequence++;knowledge.fileSequence++;}
      else if(previous?.version!==next.version){knowledge.detail=null;await openKnowledge(next.id);}
    }catch(error){if(sameSession(c)&&seq===knowledge.sequence)knowledge.error=error.message;}
    finally{if(sameSession(c)&&seq===knowledge.sequence){knowledge.loading=false;renderKnowledge();}}
  }
  async function openKnowledge(id){
    const item=knowledgeItems().find(item=>item.id===id);if(!state.user||!item)return;
    knowledge.selectedId=id;knowledge.detail=null;knowledge.detailError='';knowledge.fileId=null;knowledge.fileDetail=null;knowledge.fileError='';knowledge.fileSequence++;const seq=++knowledge.detailSequence,c={session:state.session};renderKnowledge();
    try{const data=await api('/knowledge/items/'+encodeURIComponent(id)+(item.version?'?version='+encodeURIComponent(item.version):''));if(!sameSession(c)||seq!==knowledge.detailSequence||knowledge.selectedId!==id)return;knowledge.detail=data;}
    catch(error){if(sameSession(c)&&seq===knowledge.detailSequence)knowledge.detailError=error.message;}
    finally{if(sameSession(c)&&seq===knowledge.detailSequence)renderKnowledge();}
  }
  async function openKnowledgeFile(id){
    const file=list(knowledge.detail?.files).find(item=>item.id===id&&item.type!=='folder');
    if(!state.user||!file||!file.version)return;
    const c={session:state.session},packageId=knowledge.selectedId,packageVersion=knowledge.detail.version,seq=++knowledge.fileSequence;
    knowledge.fileId=id;knowledge.fileDetail=null;knowledge.fileError='';renderKnowledge();
    try{const data=await api('/knowledge/items/'+encodeURIComponent(id)+'?version='+encodeURIComponent(file.version)+'&skill_id='+encodeURIComponent(packageId)+'&package_version='+encodeURIComponent(packageVersion));
      if(!sameSession(c)||seq!==knowledge.fileSequence||packageId!==knowledge.selectedId||packageVersion!==knowledge.detail?.version)return;
      if(data.version!==file.version)throw new APIError('文件版本已变化，请重新打开 Skill。');knowledge.fileDetail=data;
    }catch(error){if(sameSession(c)&&seq===knowledge.fileSequence)knowledge.fileError=error.message;}
    finally{if(sameSession(c)&&seq===knowledge.fileSequence)renderKnowledge();}
  }
  async function syncKnowledge(category,button,skillId=null){
    if(!admin()||knowledge.syncing)return;const c={session:state.session},key=category+(skillId?':'+skillId:'');knowledge.syncing=true;knowledge.operations[key]=knowledge.operations[key]||uid();knowledge.error='';renderKnowledge();
    try{
      const data=await api('/knowledge/sync',{method:'POST',body:{category,operation_id:knowledge.operations[key],...(skillId?{skill_id:skillId}:{})},timeout_ms:180000});if(!sameSession(c))return;
      knowledge.data=data;delete knowledge.operations[key];knowledge.detail=null;knowledge.fileId=null;knowledge.fileDetail=null;knowledge.detailSequence++;knowledge.fileSequence++;
      if(knowledge.selectedId&&!knowledgeItems().some(item=>item.id===knowledge.selectedId))knowledge.selectedId=null;
      if(knowledge.selectedId)await openKnowledge(knowledge.selectedId);
      const failed=['failed','partial','interrupted'].includes(data.sync_result?.status)||(!skillId&&list(data.sources).some(source=>(category==='all'||source.category===category)&&source.configured&&source.last_error));
      toast(failed?'部分资料未同步，已保留上次内容。':skillId?'此 Skill 已同步。':'资料已同步。',failed);
    }catch(error){if(sameSession(c)){knowledge.error=error.message;await loadKnowledge();knowledge.error=error.message;}}
    finally{if(sameSession(c)){knowledge.syncing=false;renderKnowledge();}}
  }
  function knowledgeFileTree(files){
    const root={folders:new Map(),files:[]};
    for(const file of files){
      const parts=list(file.relative_segments).length?file.relative_segments:String(file.relative_path||file.path||file.name||'').split('/').filter(Boolean);
      if(!parts.length)continue;let folder=root;
      for(const part of parts.slice(0,file.type==='folder'?parts.length:-1)){
        if(!folder.folders.has(part))folder.folders.set(part,{folders:new Map(),files:[]});folder=folder.folders.get(part);
      }
      if(file.type!=='folder')folder.files.push({file,name:parts.at(-1)});
    }
    const render=folder=>'<ul>'+[...folder.folders].sort(([a],[b])=>a.localeCompare(b,'zh-CN')).map(([name,child])=>'<li><details open><summary>'+esc(name)+'</summary>'+render(child)+'</details></li>').join('')+folder.files.sort((a,b)=>a.name.localeCompare(b.name,'zh-CN')).map(({file,name})=>'<li><button class="quiet knowledge-file'+(knowledge.fileId===file.id?' selected':'')+'" data-knowledge-file="'+esc(file.id)+'" type="button">'+esc(name)+'</button><small>'+esc(knowledgeStatus(file))+'</small></li>').join('')+'</ul>';
    return '<div class="knowledge-file-tree">'+render(root)+'</div>';
  }
  function knowledgeBody(item,content){
    const note=item?.content_status==='attachment_only'?'此项保留原件入口，正文尚未解析。':item?.content_status==='file_listing'?'此项仅保留 Skill 包文件清单，未执行。':item?.content_status==='folder'?'请在目录中选择具体文件。':'';
    return note?'<p>'+note+'</p>':'<div class="markdown-content">'+markdown(content||'正文为空。')+'</div>';
  }
  function skillPackageContent(item,detail){
    if(knowledge.detailError)return '<p class="form-error">'+esc(knowledge.detailError)+'</p>';
    if(!detail)return '<p>正在读取 Skill…</p>';
    const files=list(detail.files),primary=files.find(file=>file.id===detail.entrypoint_id),selected=knowledge.fileId?files.find(file=>file.id===knowledge.fileId):primary;
    const unavailable=!primary?(item.entrypoint_status==='ambiguous'?'存在多份 SKILL.md，请在包内文件中选择。':'未找到 SKILL.md，可查看包内文件。'):'';
    const tree='<details class="knowledge-package-files"'+(!primary?' open':'')+'><summary>包内文件（'+files.filter(file=>file.type!=='folder').length+'）</summary>'+knowledgeFileTree(files)+'</details>';
    const title=selected?'<div class="row knowledge-file-heading"><h3>'+esc(selected.relative_path||selected.name)+'</h3>'+(knowledge.fileId?'<button class="quiet" data-knowledge-main="true" type="button">查看 Skill 说明</button>':'')+'</div><div class="knowledge-meta">'+esc(knowledgeVersion(selected))+' '+knowledgeLink(selected.source_url)+'</div>':'';
    const body=knowledge.fileId?(knowledge.fileError?'<p class="form-error">'+esc(knowledge.fileError)+'</p>':!knowledge.fileDetail?'<p>正在读取文件…</p>':knowledgeBody(selected,knowledge.fileDetail.content)):primary?knowledgeBody(primary,detail.content):'';
    return (unavailable?'<p class="small-note">'+esc(unavailable)+'</p>':'')+tree+title+body;
  }
  function renderKnowledge(){
    if(!$('knowledge-categories'))return;
    const categoryContainer=$('knowledge-categories'),directory=$('knowledge-list');
    const categoryMarkup=knowledgeCategories.map(([id,title])=>'<div class="knowledge-category-group"><button class="subnav-item knowledge-category-toggle'+(knowledge.category===id?' active':'')+'" data-knowledge-category="'+id+'" type="button" role="tab" aria-selected="'+(knowledge.category===id)+'" aria-expanded="'+(knowledge.category===id&&knowledge.categoryExpanded)+'"><span>'+title+'</span><span class="category-chevron" aria-hidden="true">'+(knowledge.category===id&&knowledge.categoryExpanded?'⌄':'›')+'</span></button><div class="knowledge-category-items" data-knowledge-category-items="'+id+'"></div></div>').join('');
    // Keep the one directory node and its selected item when moving it beneath
    // the current category. Rebuilding a category must not delete that node.
    if(directory&&categoryContainer.dataset.rendered!==categoryMarkup&&categoryContainer.contains(directory))categoryContainer.after(directory);
    put('knowledge-categories',categoryMarkup);
    const categoryItems=categoryContainer.querySelector('[data-knowledge-category-items="'+knowledge.category+'"]');
    if(directory&&categoryItems&&directory.parentElement!==categoryItems)categoryItems.append(directory);
    if(directory)directory.classList.toggle('hidden',!knowledge.categoryExpanded);
    errorAt('knowledge-error',knowledge.error?(knowledge.data?'更新未完成，显示上次内容。 ':'')+knowledge.error:'');
    const source=list(knowledge.data?.sources).find(item=>item.category===knowledge.category),items=knowledgeItems(),title=knowledgeCategories.find(([id])=>id===knowledge.category)?.[1]||'共享知识';
    const configured=source?.configured===true;
    const sourceError=source?.last_error?(typeof source.last_error==='string'?source.last_error:source.last_error.message||'本次同步未完成。'):'';
    const count=items.filter(item=>item.type!=='folder'&&item.available!==false),skills=knowledge.category==='shared_skills';
    const totals=skills?count.length+' 个 Skill':'正文 '+count.filter(item=>item.content_status==='full_text').length+' 项 · 附件与包文件 '+count.filter(item=>item.content_status!=='full_text').length+' 项';
    put('knowledge-heading-controls',(admin()&&configured?'<button class="secondary" data-knowledge-sync="'+esc(knowledge.category)+'" type="button" title="'+(skills?'同步全部 Shared Skills':'同步 Selected RAG')+'"'+(knowledge.syncing||knowledge.loading||source?.status==='syncing'?' disabled':'')+'>'+(knowledge.syncing||source?.status==='syncing'?'正在同步…':'同步飞书内容')+'</button>':'')+(source&&configured?knowledgeLink(source.source_url,'打开来源文件夹 ↗'):''));
    put('knowledge-source','<h2>'+esc(title)+'</h2>'+(!source?(knowledge.loading?'<p>正在读取资料目录…</p>':'<p>资料目录尚未读取。</p>'):!configured?'<p>尚未设置来源</p>':'<p class="small-note">'+(source.last_success_at?'最近成功同步：'+date(source.last_success_at):'尚无成功同步记录')+' · '+totals+'</p>')+(source?.status==='syncing'?'<p class="small-note" role="status">正在同步，完成前保留当前内容。</p>':'')+(sourceError?'<p class="form-error" role="alert">同步未完成：'+esc(sourceError)+'</p>':''));
    const directoryItems=items.filter(item=>item.type!=='folder');
    put('knowledge-list',!configured?empty('尚未设置来源'):directoryItems.length?directoryItems.slice().sort((a,b)=>String(a.path||a.name).localeCompare(String(b.path||b.name),'zh-CN')).map(item=>'<button class="knowledge-entry secondary'+(knowledge.selectedId===item.id?' selected':'')+'" data-knowledge-open="'+esc(item.id)+'" type="button" title="'+esc(item.name||item.path)+'"><strong>'+esc(item.name||String(item.path||'').split('/').pop())+'</strong></button>').join(''):empty(knowledge.loading?'正在读取目录':'尚无同步内容',admin()?'点击“同步飞书内容”读取来源。':'请联系管理员同步资料。'));
    const currentItem=items.find(item=>item.id===knowledge.selectedId),detail=knowledge.detail;
    if(!currentItem){put('knowledge-content',empty(skills?'选择一个 Skill 查看':'选择一份资料查看'));return;}
    const item=detail?.item||currentItem;
    const isSkill=item.content_status==='skill_package';
    put('knowledge-content','<div class="row"><h2>'+esc(item.name||item.path)+'</h2>'+(isSkill&&admin()?'<button class="secondary" data-knowledge-sync="shared_skills" data-knowledge-skill="'+esc(item.id)+'" type="button" title="仅同步当前 Skill"'+(knowledge.syncing||source?.status==='syncing'?' disabled':'')+'>同步飞书内容</button>':'')+'</div><div class="knowledge-meta"><span>'+esc(knowledgeStatus(item))+'</span>'+(isSkill?'<span>'+esc(item.file_count??list(detail?.files).filter(file=>file.type!=='folder').length)+' 个文件</span>':'')+(item.version?'<span>'+esc(knowledgeVersion(item))+'</span>':'')+knowledgeLink(item.source_url,isSkill?'打开 Skill 文件夹 ↗':'打开飞书原文 ↗')+'</div>'+(/草稿|测试/.test(item.source_status||'')?'<p class="small-note">'+esc(item.source_status)+'</p>':'')+(item.version?'<details class="knowledge-version"><summary>版本详情</summary><p class="mono">'+esc(item.version)+'</p></details>':'')+(item.available===false?'<p class="notice">来源已不可用，当前展示保留的历史内容。</p>':'')+(item.last_error?'<p class="form-error">'+esc(item.last_error)+'</p>':'')+(isSkill?skillPackageContent(item,detail):knowledge.detailError?'<p class="form-error">'+esc(knowledge.detailError)+'</p>':!detail?'<p>正在读取正文…</p>':knowledgeBody(item,detail.content)));
  }
  function maintenanceCard(run,details=false){
    if(!run)return empty('尚无检查简报');
    const report=run.inspection||{},issues=list(report.issues),isActive=['queued','running','draining'].includes(run.status);
    return '<article class="item-card" data-key="maintenance:'+esc(run.id)+'"><div class="row"><b>'+esc(run.kind==='inspection'?'系统检查':'飞书全量刷新')+'</b>'+pill(run.status==='partial'?'部分完成':run.status,isActive?'warn':'')+'</div><p>'+esc(run.summary||(isActive?'已启动，尚未完成。':'结果待核对。'))+'</p><p class="small-note">'+date(run.completed_at||run.created_at)+(run.missed?' · 恢复后补跑':'')+'</p>'+issues.map(i=>'<p class="form-error">'+esc(i.summary)+'</p>').join('')+(report.projects?'<p>已查看 '+esc(report.projects.length)+' 个项目。</p>':'')+(run.kind!=='inspection'?'':details?'<details><summary>同步与检查详情</summary><p>同步阶段处理有效回执；诊断阶段只读。</p><pre>'+esc(safeJSON({sources:run.results,inspection:run.inspection,stage:run.stage}))+'</pre></details>':'<button class="quiet" data-rodos-run="'+esc(run.id)+'" type="button">查看详情</button>')+'</article>';
  }
  function renderRodos(){
    const data=rodos.maintenance;
    if(data){
      put('rodos-schedules',list(data.schedules).map(s=>'<article class="item-card"><div class="row"><b>'+esc(s.id==='hourly_refresh'?'每小时全量刷新':'周五 23:00 系统检查')+'</b><button class="quiet" data-rodos-schedule="'+esc(s.id)+'" type="button">'+(s.enabled?'停用':'启用')+'</button></div><p>'+esc(s.enabled?(data.paused?'随中央暂停':'已启用'):'已停用')+' · 北京时间</p><p class="small-note">下次：'+date(s.next_due_at)+' · 上次：'+date(s.last_run_at||s.last_completed_at)+'</p></article>').join(''));
      put('rodos-active-maintenance',data.active_run?maintenanceCard(data.active_run):'<p class="small-note">当前没有正在执行的维护。</p>');
      put('rodos-latest-inspection',maintenanceCard(data.latest_inspection));
      put('rodos-inspection-history',rodos.runs.filter(r=>r.kind==='inspection').map(r=>maintenanceCard(r)).join('')||empty('尚无历史检查'));
    }
    show('rodos-agent-editor',rodos.tab==='agent');show('rodos-memory-editor',rodos.tab==='memory');
    for(const [id,tab] of [['rodos-agent-tab','agent'],['rodos-memory-tab','memory']]){const b=$(id);if(b){b.setAttribute('aria-selected',String(rodos.tab===tab));b.classList.toggle('secondary',rodos.tab===tab);b.classList.toggle('quiet',rodos.tab!==tab);}}
    if(rodos.policy){
      setText('rodos-policy-meta','版本 '+rodos.policy.version+' · '+date(rodos.policy.updated_at)+' · 下一次对话运行生效');
      if(!rodos.policyDirty)$('rodos-policy-content').value=rodos.policy.content;
    }
    const scopeText=m=>m.applicability?.kind==='account'?'个人偏好':m.scope==='project'?projectName(m.project_id):'机构';
    const visible=rodos.memories.filter(m=>$('rodos-memory-inactive')?.checked||['active','candidate'].includes(m.status));
    put('rodos-memory-items',visible.map(m=>'<article class="item-card" data-key="memory:'+esc(m.id)+'"><div class="row"><h3>'+esc(m.topic||'已忘记的约定')+'</h3>'+pill(({active:'有效',candidate:'待核对依据',inactive:'已失效',forgotten:'已忘记'})[m.status]||m.status)+'</div><p>'+esc(m.content)+'</p><p class="small-note">'+esc(scopeText(m))+' · '+esc(m.evidence_kind==='explicit'?'明确约定':'归纳或推断')+' · 版本 '+esc(m.version)+'</p>'+(m.status!=='forgotten'?'<div class="item-actions"><button class="quiet" data-memory-edit="'+esc(m.id)+'" type="button">修改</button><button class="quiet" data-memory-invalidate="'+esc(m.id)+'" type="button">失效</button><button class="quiet danger" data-memory-forget="'+esc(m.id)+'" type="button">忘记</button></div>':'')+'</article>').join('')||empty('暂无长期记忆'));
    setText('rodos-memory-preview',rodos.preview?.available?rodos.preview.content:rodos.preview?.error||'摘要尚未生成。');
    const projectSelect=$('rodos-memory-project');if(projectSelect){const old=projectSelect.value;projectSelect.innerHTML=state.projects.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+'</option>').join('');if(state.projects.some(p=>p.id===old))projectSelect.value=old;}
    if(rodos.edit){$('rodos-memory-scope').disabled=!!rodos.edit.id;show('rodos-memory-project-label',$('rodos-memory-scope').value==='project');}
  }
  async function loadRodos(forceSettings=false){
    if(!admin()||rodos.loading)return;const c=context(),seq=++rodos.sequence;rodos.loading=true;
    const settings=state.page==='global'&&(!rodos.settingsLoaded||forceSettings);
    const paths=['/maintenance/state','/maintenance/runs','/peer/nodes',...(settings?['/settings/agent','/settings/memories?include_inactive=true','/settings/memory-preview']:[])];
    const values=await Promise.allSettled(paths.map(p=>api(p)));
    if(!sameSession(c)||seq!==rodos.sequence)return;
    rodos.loading=false;const errors=[];
    values.forEach((r,i)=>{if(r.status==='rejected'){errors.push(r.reason.message);return;}const d=r.value;
      if(i===0)rodos.maintenance=d;else if(i===1)rodos.runs=list(d.items);else if(i===2)rodos.nodes=list(d.nodes);else if(i===3)rodos.policy=d;else if(i===4)rodos.memories=list(d.items);else if(i===5)rodos.preview=d;
    });
    if(settings&&values.slice(3).every(v=>v.status==='fulfilled'))rodos.settingsLoaded=true;
    errorAt(state.page==='global'?'rodos-settings-error':'rodos-agent-maintenance-error',errors.join(' '));renderRodos();renderPeople();
  }
  function editMemory(item=null){
    if(rodos.memoryDirty&&!window.confirm('放弃尚未保存的记忆修改？'))return;
    rodos.edit=item?{...item}:{};rodos.memoryDirty=false;rodos.memoryOp=null;
    $('rodos-memory-id').value=item?.id||'';$('rodos-memory-topic').value=item?.topic||'';$('rodos-memory-content').value=item?.content||'';
    $('rodos-memory-scope').value=item?.applicability?.kind==='account'?'account':item?.scope||'organization';
    if(item?.project_id)$('rodos-memory-project').value=item.project_id;
    show('rodos-memory-form');errorAt('rodos-memory-error','');renderRodos();$('rodos-memory-topic').focus();
  }
  async function changeMemory(kind,item){
    const c=context(),body={operation:kind,operation_id:uid(),expected_version:item.version,memory_id:item.id,topic:item.topic,content:item.content,scope:item.applicability?.kind==='account'?'account':item.scope,project_id:item.project_id||''};
    await api('/settings/memories',{method:'POST',body});if(!sameSession(c))return;
    setText('rodos-memory-status',kind==='forget'?'已忘记，不再从原来源重新提取。历史审计与备份仍保留。':'该约定已失效。');await loadRodos(true);
  }
  async function startMaintenance(kind,button){
    await busy(button,async()=>{const c=context(),op=rodos.maintenanceOps[kind]||(rodos.maintenanceOps[kind]=uid());const run=await api('/maintenance/'+kind,{method:'POST',body:{operation_id:op}});if(!sameSession(c))return;
      if(!run.request_not_started)delete rodos.maintenanceOps[kind];
      toast(run.summary||(['queued','running'].includes(run.status)?'已启动，尚未完成。':'请查看实际运行结果。'));await loadRodos();
    },state.page==='global'?'rodos-maintenance-error':'rodos-agent-maintenance-error');
  }
  async function approvePeer(nodeId,requestId,button){
    const node=rodos.nodes.find(n=>n.node_id===nodeId),request=list(node?.pending_requests).find(r=>r.request_id===requestId);
    if(!request)return;
    const pending=request.status==='registry_pending';
    confirm(pending?'核对节点登记？':node.bound?'更换这个节点的身份？':'确认这个节点的接入申请？',pending?'只核对原确认结果，不重新生成节点身份。':'请核对节点对话中的登记码 '+request.confirmation_code+'。私钥始终留在节点，不需要传递文件。'+(node.bound?'原身份将失效。':''),pending?'核对登记':'确认绑定',async()=>{
      const c=context(),key='peer:'+requestId,op=request.resume_operation_id||rodos.maintenanceOps[key]||(rodos.maintenanceOps[key]=uid());
      const body={operation_id:op,request_id:requestId,expected_request_hash:request.request_hash};
      if(!pending)body.rotate=!!node.bound;
      const result=await api('/peer/nodes/'+encodeURIComponent(nodeId)+'/approve-enrollment',{method:'POST',body});if(!sameSession(c))return;
      if(result.readback_verified)delete rodos.maintenanceOps[key];
      toast(result.readback_verified?'节点归属已确认。中央已发布登记，节点下次检查会自动完成核对。':'归属确认已保存，登记结果仍待核对；不需要重新生成身份。');await loadRodos();
    });
  }
  function peerBindingButton(nodeId){
    const record=rodos.nodes.find(n=>n.node_id===nodeId);
    if(!record)return '';
    if(record.runtime_available===false)return '<span class="small-note">'+esc(record.disabled_reason||'互派暂未启用')+'</span>';
    const pending=list(record.pending_requests).filter(r=>['pending','registry_pending'].includes(r.status));
    const failure=pending.find(r=>['conflict','failed'].includes(r.automatic_status)),abnormal=['conflict','failed'].includes(record.registration_status)||!!record.registration_error||!!failure;
    const error=record.registration_error||failure?.message||'';
    const registering=record.registration_status==='registry_pending'||pending.some(r=>r.status==='registry_pending');
    const label=record.ready?'互派已启用':record.bound?'登记完成（互派待就绪）':abnormal?'登记异常':registering?'正在登记':pending.length?'待自动登记':'互派未启用';
    return '<span class="small-note">'+esc(label)+'</span>'+(abnormal&&error?'<span class="small-note">'+(label==='登记异常'?'':'登记异常：')+esc(error)+'</span>':'');
  }
  function renderPeople(){
    put('people-list',!state.loaded.users&&!state.overview?unavailable():allUsers().map(u=>'<article class="item-card member-card'+(u.enabled===false?' member-inactive':'')+'" data-key="user:'+esc(u.id)+'"><div class="member-identity"><h3>'+esc(displayName(u))+'</h3><small>'+esc(u.username)+'</small><div class="badge-row">'+list(u.roles).map(r=>pill(r)).join('')+(u.enabled===false?pill('已停用'):'')+(developerMode&&u.identity_mode==='role_test'?pill('角色测试','warn'):'')+'</div></div><div class="member-actions" role="group" aria-label="'+esc(displayName(u))+'的账号操作"><button class="quiet" data-edit-user="'+esc(u.id)+'" type="button">编辑资料</button>'+(u.id!==state.user?.id&&u.enabled!==false?'<button class="quiet" data-reset-password="'+esc(u.id)+'" type="button">重置密码</button>':'')+'<button class="quiet'+(u.enabled===false?'':' danger')+'" data-user-enabled="'+esc(u.id)+'" type="button">'+(u.enabled===false?'重新启用':'停用账号')+'</button></div>'+memberNodes(u).map(node=>'<div class="member-node"><div class="member-node-heading"><b>'+esc(node.display_name||node.label||'Local Agent')+'</b><small>'+esc(phase3NodeId(node))+'</small></div><div class="member-node-actions"><button class="secondary" data-phase3-copy="'+esc(phase3NodeId(node))+'" type="button">复制接入短指令</button>'+peerBindingButton(phase3NodeId(node))+'</div></div>').join('')+'</article>').join('')||empty('暂无成员记录'));
  }
  function setMemberEnabled(user){
    if(!admin()||!user)return;const c=context(),enabled=user.enabled===false;
    confirm(enabled?'重新启用「'+displayName(user)+'」？':'停用「'+displayName(user)+'」的账号？',enabled?'该账号可以重新登录。':'该账号将无法登录 Panel，节点任务保持原状。',enabled?'重新启用':'停用账号',async()=>{
      const result=await api('/users/'+encodeURIComponent(user.id),{method:'PATCH',body:{enabled}});if(!sameSession(c))return;
      if(user.id===state.user.id&&(!enabled||result.relogin_required)){state.loginRecovery=null;resetSession('账号状态已修改，请重新登录。');return;}
      await refresh();toast(enabled?'账号已启用。':'账号已停用，节点任务保持原状。');
    });
  }

  function renderRuntime(){
    const runtime=state.overview?.runtime||{};
    const configured=labInfo?.runtime?.model_mode==='agent_plan';
    put('runtime-state','<dl class="runtime-list"><dt>中央模型</dt><dd>'+esc(configured?'已连接 Agent Plan':'连接状态待核对')+'</dd><dt>任务与协作记录</dt><dd>使用已登记飞书入口</dd><dt>节点检查</dt><dd>由各工作节点按自己的检查安排读取当前任务</dd></dl>');
    if(developerMode){
      put('runtime-state',$('runtime-state').innerHTML+'<dl class="runtime-list"><dt>环境标识</dt><dd class="mono">'+esc(labInfo?.env_id)+'</dd><dt>运行代次</dt><dd class="mono">'+esc(labInfo?.generation)+'</dd><dt>中央长期调度</dt><dd>关闭</dd></dl>');
      put('project-health-list',list(runtime.project_health).map(h=>'<article class="item-card"><h3>'+esc(projectName(h.project_id))+'</h3>'+pill(h.needs_attention?'待处理':h.status||'待核对')+'<pre>'+esc(safeJSON(h))+'</pre></article>').join('')||empty('尚无项目检查记录'));
    }
    $('toggle-runtime').disabled=true;
  }

  function renderDiagnostics(){
    const d=state.diagnostics,o=d?.online||{},l=d?.local_agents||{};
    setText('diagnostics-status',state.diagnosticsLoading?(d?'正在更新，当前显示上次结果。':'正在检查运行记录…'):d?'观察时间：'+date(d.observed_at):'');
    errorAt('diagnostics-error',state.diagnosticsError);
    $('refresh-diagnostics').disabled=state.diagnosticsLoading;
    if(!d){put('diagnostics-result','');return;}
    const on=o.onboarding||{},m=o.model||{},t=o.tools||{},f=o.feishu||{},tasks=o.tasks||{},s=o.central_schedule||{};
    put('diagnostics-result','<p class="small-note">'+esc(d.scope||'本机已保存记录')+'</p>'+
      '<article class="item-card"><h3>合成任务</h3><p>节点接入保持关闭；本页不判定真实成员可以开始工作。</p><p>已知测试任务 '+esc(tasks.known??0)+' 项；待审阅草稿 '+esc(tasks.pending_drafts??0)+' 项。</p></article>'+
      '<article class="item-card"><h3>中央运行与本地测试后端</h3><p>模型最近成功：'+date(m.last_success_at)+'；工具最近失败：'+date(t.last_failure_at)+'。</p><p>测试后端最近读取：'+date(f.last_full_read_at)+'；待核对写入 '+esc(f.unresolved_operations??0)+' 项。</p><p>受控检查最近运行：'+date(s.last_actual_run_at)+'（'+esc(label(s.last_result||'未验证'))+'）。</p></article>'+
      '<article class="item-card"><h3>合成反馈与客户端验收</h3><p>本地测试后端已读回合成反馈 '+esc(l.feedback_readback_records??0)+' 条。</p><p>真实客户端接入、读取、反馈与定时执行均待验证；上述数量不能作为客户端验收。</p></article>'+
      '<details><summary>查看诊断限制</summary>'+list(d.limits).map(x=>'<p>'+esc(x)+'</p>').join('')+'</details>');
  }
  async function loadDiagnostics(){
    if(!developerMode||!admin()||state.diagnosticsLoading)return;
    const c={session:state.session},seq=++state.diagnosticsSeq;state.diagnosticsLoading=true;state.diagnosticsError='';renderDiagnostics();
    try{const data=await api('/diagnostics');if(!sameSession(c)||seq!==state.diagnosticsSeq)return;if(!data?.online||!data?.local_agents)throw new APIError('诊断结果不完整，请重新检查。');state.diagnostics=data;}
    catch(e){if(sameSession(c)&&seq===state.diagnosticsSeq)state.diagnosticsError=(state.diagnostics?'更新失败，当前显示上次结果。':'诊断暂时无法读取。')+' '+readableError(e?.message);}
    finally{if(sameSession(c)&&seq===state.diagnosticsSeq){state.diagnosticsLoading=false;renderDiagnostics();}}
  }
  function renderSources(){put('source-list',empty('仅使用本地测试材料','中央可读取已登记的合成材料。节点连接、真实飞书及其他资料入口保持关闭。'));}

  function runContent(run,brief=false){
    const failed=['failed','denied','cancelled','interrupted','paused','uncertain','needs_attention'].includes(run.status);
    const title=run.trigger==='scheduled'?'周期检查':run.kind==='sync'?'检查一次':'对话处理';
    const basic='<div class="run-heading"><b>'+title+'</b>'+pill(run.no_analysis?'无变化未分析':run.status,failed?'error':running(run)?'warn':'')+'</div><p class="small-note">'+date(run.created_at)+'</p>'+(run.error?'<p class="run-error">'+esc(runFailure(run))+'</p>':'');
    if(!developerMode)return basic;
    return basic+(failed?'<button class="secondary" data-retry-run="'+esc(run.id)+'" type="button">继续这次处理</button>':'')+'<details><summary>查看执行详情</summary>'+(run.result?'<div class="markdown-content">'+markdown(textValue(run.result))+'</div>':'')+'<p>已记录 '+esc(run.usage?.tool_calls??list(run.steps).filter(s=>s.type==='tool_call').length)+' 次工具处理、'+esc(run.usage?.model_calls??0)+' 次模型调用。</p>'+list(run.steps).map(step=>'<div class="tool-step">'+stepSources(step)+'<details><summary>工具与技术记录</summary><pre>'+esc(safeJSON(step))+'</pre></details></div>').join('')+'<p class="mono">'+esc(run.id)+'</p>'+(run.technical_detail?'<pre>'+esc(textValue(run.technical_detail))+'</pre>':'')+'</details>';
  }

  function renderRuns(){
    const runs=state.runItems.length?state.runItems:[...new Map([...list(state.overview?.runs),...Object.values(state.workspaces).flatMap(w=>list(w?.runs))].map(r=>[r.id,r])).values()].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')));
    put('run-list',runs.map(run=>'<article class="item-card" data-key="run:'+esc(run.id)+'"><button class="quiet" data-run="'+esc(run.id)+'" type="button">'+date(run.created_at)+' · '+esc(run.project_id?projectName(run.project_id):'机构讨论')+' · '+esc(run.trigger==='scheduled'?'受控定时检查':run.kind==='sync'?'检查一次':'对话处理')+' · '+esc(run.no_analysis?'无变化未分析':label(run.status))+'</button></article>').join('')||empty('暂无运行记录'));
    put('chosen-run-detail',state.selectedRun?runContent(state.selectedRun):'');show('chosen-run-detail',!!state.selectedRun);
    show('load-more-runs',state.runNext!==null);$('load-more-runs').disabled=state.runsLoading;
  }
  async function loadRuns(more=false){
    if(!developerMode||state.runsLoading)return;const c={session:state.session};state.runsLoading=true;
    try{const response=await api('/runs?limit=30&offset='+(more?state.runNext||0:0));if(!sameSession(c))return;
      state.runItems=more?[...state.runItems,...list(response.items)]:list(response.items);state.runNext=response.next_offset;state.runTotal=response.total;
      setText('run-list-status','已显示 '+state.runItems.length+' / '+state.runTotal+' 条记录。');
    }catch(e){if(sameSession(c))setText('run-list-status',e.message);}finally{state.runsLoading=false;if(sameSession(c))renderRuns();}
  }
  function renderOnboarding(){
    const o=state.onboarding,selected=!!o?.choice,ready=!!o?.ready_for_work;
    const unresolved=o?.stage_status==='blocked'&&!list(o.missing).length?'这次检查没有形成明确的补充项，请重试检查。':'';
    const error=state.checking?'':state.onboardingCheckError||o?.error||state.onboardingError||state.errors.onboarding||unresolved;
    const nextStage=nextOnboardingStep(o),continueStep=!!nextStage&&!state.instruction;
    show('onboarding-paths',!selected||state.reentering);show('onboarding-reentry-note',selected&&state.reentering);show('onboarding-current',!state.reentering&&(selected||!!error));show('onboarding-actions',selected&&!state.reentering);$('onboarding-mode-label').parentElement.classList.toggle('hidden',!selected);errorAt('onboarding-path-error',state.reentering?error:'');
    setText('onboarding-go-project','进入我的项目');setText('onboarding-mode-label',o?.choice==='refresh'?'更新已有接入':'首次接入');
    $('check-onboarding').disabled=!selected||state.checking||!!o?.checking;
    setText('check-onboarding',state.checking||o?.checking?'正在检查…':error?'重试检查':ready?'检查最新反馈':'检查结果');
    setText('onboarding-check-hint',error?'':ready?'有新反馈时点击':continueStep?'下一步完成后再检查':'Agent 完成本步后点击');
    show('onboarding-check-hint',selected&&!error);
    show('onboarding-go-project',ready);show('continue-onboarding',selected&&!ready&&continueStep&&!error);show('copy-onboarding-instruction',selected&&!ready&&!continueStep&&!error);show('onboarding-check-action',selected);
    setText('continue-onboarding','继续：'+(nextStage===1?'确认工作范围':'设置定时同步'));
    $('continue-onboarding').disabled=!!state.instructionPromise;
    errorAt('onboarding-action-error',error?'':state.instructionError);
    errorAt('onboarding-check-error',error);
    if(!o){put('onboarding-status',empty(error?'接入状态暂时无法读取':'正在读取接入状态',error?'已有记录保留，请刷新重试。':''));show('onboarding-paths',false);show('onboarding-current',true);for(const id of ['onboarding-progress','onboarding-actions','onboarding-instruction','onboarding-check-action','onboarding-check-details'])show(id,false);put('onboarding-link','');setText('onboarding-prompt','');setText('onboarding-action-status','');errorAt('onboarding-action-error','');return;}
    const steps=[{stage:0,name:'连接飞书',title:'连接你的飞书',description:'让 Agent 检查飞书连接，并完成指定位置的读写测试。'},{stage:1,name:'工作范围',title:'确认你的工作范围',description:'在 Agent 中确认身份、本地工作范围和允许共享的内容。'},{stage:3,name:'定时同步',title:'设置定时同步',description:'设置同步计划，并手动完成一次读取、反馈和读回。'}];
    const stage=Number(o.stage)||0,displayStage=stage===2?3:stage;
    const completed=new Set(list(o.completed_steps??o.history).map(h=>Number(typeof h==='object'?h.stage:h)));
    show('onboarding-progress',selected);show('onboarding-check-details',selected);show('onboarding-check-action',selected);put('onboarding-progress',steps.map((step,i)=>'<li class="'+(ready||completed.has(step.stage)?'complete':displayStage===step.stage?'active':'')+'"'+(!ready&&displayStage===step.stage?' aria-current="step"':'')+'><span>'+(ready||completed.has(step.stage)?'✓':i+1)+'</span>'+step.name+'</li>').join(''));
    const responsibility=o.required_actor||o.next_actor;
    let status=error?'':o.checking||state.checking?'正在检查':ready?'接入完成':continueStep?'':o.stage_status==='blocked'?(responsibility==='user'?'需要你确认':responsibility==='agent'?'需要 Agent 补充':'还需补充'):o.stage_status==='verified'?'本步完成':'等待 Agent 完成';
    let description=error?'':ready?'现在可以进入项目工作。':continueStep?'点击下方按钮，获取这一步给 Agent 的指令。':o.choice==='refresh'&&stage>0?'请让 Agent 只检查缺失的部分。':steps.find(s=>s.stage===displayStage)?.description||'请按当前步骤继续。';
    if(!ready&&o.stage_status==='blocked'&&list(o.missing).length&&!error){
      const first=String(textValue(list(o.missing)[0]));
      description=stage===1&&/身份|角色|共享范围|本地读取范围/.test(first)
        ?'请向你的 Agent 明确确认：你是谁、参与什么工作，以及本机可读取和分享的范围。'
        :first;
    }
    put('onboarding-status','<h2>'+esc(ready?'可以开始工作':continueStep&&!error?'上一步已完成':steps.find(s=>s.stage===displayStage)?.title||'当前接入步骤')+'</h2>'+(description?'<p>'+esc(description)+'</p>':'')+(status?'<p class="onboarding-state">'+esc(status)+'</p>':''));
    const instruction=state.instruction,old=expired(instruction);show('onboarding-instruction',selected&&!ready&&!error&&!!instruction&&!old);
    show('prepare-onboarding-instruction',selected&&!ready&&!error&&!continueStep&&(!instruction||!!state.instructionError)&&!state.instructionPromise);
    setText('onboarding-instruction-summary','复制后，粘贴到你的 Agent 对话中。');
    if(instruction){setText('onboarding-prompt',instruction.prompt);$('onboarding-link').title='有效至 '+date(instruction.expires_at);put('onboarding-link',link(instruction.url,'步骤说明'));}
    state.copied=copiedValue();
    setText('copy-onboarding-instruction',old?'重新生成指令':o.stage_status==='blocked'&&list(o.missing).length?'复制补充指令':'复制给我的 Agent');
    $('copy-onboarding-instruction').disabled=!instruction||!!state.instructionPromise;
    setText('onboarding-action-status',ready?'':error?'':old?'原指令已过期，重新生成后再复制。':state.instructionPromise?'正在准备指令…':continueStep?'':!instruction?'指令尚未生成，请重新生成。':state.copied?'已复制，接下来请粘贴给 Agent。':'');
    const missing=list(o.missing),next=ready?'进入项目。':error?'重试检查。':continueStep?'获取下一步指令。':missing.length?'完成这一项后，再检查结果。':'Agent 完成本步后，检查结果。';
    const diagnosis=admin()?'<details><summary>管理员诊断</summary><div class="markdown-content">'+markdown(o.summary)+'</div>'+evidenceContent(o.evidence||[])+'<pre>'+esc(safeJSON({session_id:o.session_id,stage:o.stage,stage_status:o.stage_status,completed_steps:o.completed_steps,history:o.history,technical_detail:o.technical_detail}))+'</pre></details>':'';
    put('onboarding-check-report','<p>检查时间：'+(o.checked_at?date(o.checked_at):'尚未检查')+'</p>'+(missing.length>1?'<p>其余待补充：'+esc(missing.slice(1).map(textValue).join('；'))+'</p>':'')+'<p>下一步：'+esc(next)+'</p>'+diagnosis);
  }
  async function prepareInstruction(notify=true){
    if(state.instructionPromise)return state.instructionPromise;const c=onboardingContext(),signature=onboardingKey(state.onboarding),seq=++state.instructionSeq;
    const pending=(async()=>{const data=await api('/onboarding/instruction',{method:'POST',body:{purpose:'onboarding'}});if(!currentOnboarding(c)||seq!==state.instructionSeq||signature!==onboardingKey(state.onboarding))return;state.instruction=data;state.instructionError='';if(notify)setText('onboarding-action-status','指令已准备，可以复制给 Agent。');return data;})();
    state.instructionPromise=pending;renderOnboarding();
    try{return await pending;}finally{if(state.instructionPromise===pending){state.instructionPromise=null;if(currentOnboarding(c))renderOnboarding();}}
  }
  async function requestOnboardingInstruction(){
    const c=onboardingContext(),step=nextOnboardingStep(state.onboarding),key=continuationKey(state.onboarding);
    try{const guide=await prepareInstruction();if(guide&&step&&currentOnboarding(c)&&Number(state.onboarding?.stage)===step){try{sessionStorage.setItem(key,'yes');}catch{}}}
    catch(e){if(currentOnboarding(c)){state.instructionError=e.message;renderOnboarding();}}
  }
  async function reloadOnboardingStatus(){
    const c=onboardingContext(),seq=++state.onboardingRequestSeq;
    try{const data=await api('/onboarding/status');if(!currentOnboarding(c)||seq!==state.onboardingRequestSeq)return;state.onboardingError='';state.onboardingCheckError='';delete state.errors.onboarding;acceptOnboarding(data);}
    catch(e){if(currentOnboarding(c)&&seq===state.onboardingRequestSeq)state.onboardingCheckError=e.message;}
    finally{if(currentOnboarding(c)&&seq===state.onboardingRequestSeq)renderOnboarding();}
  }
  async function checkOnboarding(manual=false){
    if(state.checking||state.reentering||state.onboardingStarting||!state.onboarding?.choice)return;
    const c=onboardingContext(),sessionId=state.onboarding.session_id,expectedStage=Number(state.onboarding.stage),seq=++state.onboardingRequestSeq;
    let reload=false;state.checking=true;state.lastCheck=Date.now();state.onboardingCheckError='';state.instructionError='';renderOnboarding();
    try{const data=await api('/onboarding/check',{method:'POST',timeout_ms:210000,body:{session_id:sessionId,expected_stage:expectedStage}});
      if(!currentOnboarding(c)||seq!==state.onboardingRequestSeq||state.onboarding?.session_id!==sessionId||Number(state.onboarding.stage)!==expectedStage)return;
      delete state.errors.onboarding;state.onboardingError='';state.onboardingCheckError='';acceptOnboarding(data);
      if(manual&&!data.error)setText('onboarding-action-status',data.ready_for_work?'接入完成，可以进入项目。':nextOnboardingStep(data)?'本步已完成，请继续下一步。':'检查结果已更新。');
    }catch(e){if(currentOnboarding(c)&&seq===state.onboardingRequestSeq&&state.onboarding?.session_id===sessionId){state.onboardingCheckError=e.message;reload=e.status===409;}}
    finally{if(currentOnboarding(c)&&seq===state.onboardingRequestSeq){state.checking=false;renderOnboarding();}}
    if(reload)await reloadOnboardingStatus();
  }
  function maybeCheck(){/* The member checks after their Agent completes this step. */}
  function projectFolderLink(value,title='打开项目工作文件夹 ↗'){
    if(!state.projects.some(p=>[p.folder_url,p.resolved_folder_url].includes(value))&&value!==state.defaultFolderUrl)return '';
    try{const u=new URL(value);if(u.protocol!=='https:'||u.username||u.password||u.port||!u.hostname.endsWith('.feishu.cn')||!/^\/drive\/folder\/[A-Za-z0-9]+\/?$/.test(u.pathname))return '';}
    catch{return '';}
    return '<a href="'+esc(value)+'" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">'+esc(title)+'</a>';
  }
  function renderProjects(){
    const projects=state.projects.filter(p=>state.showInactive||p.status!=='inactive').slice().sort((a,b)=>Number(!!b.is_default)-Number(!!a.is_default));
    if(navigation.projectId&&!projects.some(p=>p.id===navigation.projectId))navigation.projectId=null;
    put('project-navigation',projects.map(p=>'<button type="button" class="sidebar-item sidebar-child'+(navigation.projectId===p.id?' active':'')+'" data-project-view-id="'+esc(p.id)+'" aria-current="'+(navigation.projectId===p.id?'page':'false')+'">'+esc(p.name)+(p.status==='inactive'?'（已停用）':'')+'</button>').join('')||'<p class="small-note">暂无项目</p>');
    const displayed=navigation.projectId?projects.filter(p=>p.id===navigation.projectId):projects;
    setText('project-selection-heading',navigation.projectId?projects.find(p=>p.id===navigation.projectId)?.name:'管理项目');
    show('project-instruction',false);
    put('project-list',state.errors.projects&&!state.loaded.projects?empty('项目暂时无法读取','请稍后重新打开项目管理。'):displayed.length?displayed.map(p=>{
      const inactive=p.status==='inactive',stopping=p.status==='deactivating',selected=p.id===state.projectId;
      const folder=p.resolved_folder_url||p.folder_url||state.defaultFolderUrl;
      const members=list(p.node_ids).map(phase3NodeName).join('、')||'尚未关联节点';
      const status=p.is_default?pill('Default'):inactive?pill('已停用'):stopping?pill('停用处理中','warn'):'';
      const progress=p.deactivation&&stopping?'<p class="notice" role="status">已暂停 '+list(p.deactivation.paused_task_ids).length+' 项任务；'+list(p.deactivation.pending_task_ids).length+' 项仍待处理。'+esc(p.deactivation.error?' '+readableError(p.deactivation.error):'')+'</p>':'';
      return '<article class="panel project-card'+(selected?' selected-project':'')+'" data-key="project:'+esc(p.id)+'"><div class="row"><h2>'+esc(p.name)+'</h2><div class="badge-row">'+status+(selected?pill('当前项目'):'')+'</div></div>'+(p.description?'<p class="project-summary">'+esc(summary(p.description,125))+'</p>'+(p.description.length>125?'<details><summary>查看项目说明</summary><div class="markdown-content">'+markdown(p.description)+'</div></details>':''):'<p class="project-summary muted">暂无项目说明</p>')+'<p class="small-note">参与节点：'+esc(members)+'</p><div class="project-links">'+projectFolderLink(folder)+'</div>'+(p.folder_source==='default'||!p.folder_url?'<p class="small-note">使用 Default 项目的工作文件夹。</p>':'')+progress+'<div class="item-actions"><button class="primary" data-project-work="'+esc(p.id)+'" type="button">查看项目工作</button>'+(admin()?'<button class="quiet" data-project-chat="'+esc(p.id)+'" type="button">讨论此项目</button><button class="quiet" data-edit-project="'+esc(p.id)+'" type="button">编辑项目</button>'+(!p.is_default?(inactive?'<button class="secondary" data-reactivate-project="'+esc(p.id)+'" type="button">重新启用</button>':'<button class="quiet danger" data-deactivate-project="'+esc(p.id)+'" type="button">'+(stopping?'继续停用':'停用项目')+'</button>'):''):'')+'</div></article>';
    }).join(''):empty(state.loaded.projects?'暂无相关项目':'正在读取项目',state.loaded.projects?(admin()?'点击“创建项目”开始。':'请联系管理员将你加入相关项目。'):''));
    renderSidebar();
  }

  async function selectProject(id,page=state.page){
    if(!state.projects.some(p=>p.id===id)){showError('该项目已不可访问，请刷新项目列表后重新选择。');renderNavigation();return;}
    if(id===state.projectId){state.page=page;render();if(page==='collaboration')await loadPhase3();return;}
    clearProject();state.projectId=id;state.page=page;state.selectionNotice='';state.workspace=state.workspaces[id]||null;render();await refresh();
  }
  async function prepareProject(id){
    if(!state.projects.some(p=>p.id===id)){showError('该项目已不可访问，请刷新项目列表后重试。');return;}
    if(state.projectInstruction?.loading&&state.projectInstruction.project_id===id)return;
    const session=state.session,seq=++state.projectInstructionSeq;
    state.projectInstruction={project_id:id,loading:true};renderProjects();
    try{const data=await api('/onboarding/instruction',{method:'POST',body:{project_id:id,purpose:'project'}});if(state.session!==session||seq!==state.projectInstructionSeq)return;state.projectInstruction={...data,project_id:id};}
    catch(e){if(state.session===session&&seq===state.projectInstructionSeq)state.projectInstruction={project_id:id,error:e.message};}
    finally{if(state.session===session&&seq===state.projectInstructionSeq){renderProjects();$('project-instruction')?.scrollIntoView?.({behavior:'smooth',block:'nearest'});}}
  }
  async function trackRun(id){
    if(state.polling.has(id))return;const c={session:state.session};state.polling.set(id,null);
    const poll=async()=>{
      if(!sameSession(c))return;
      try{const response=await api('/runs/'+encodeURIComponent(id)),run=response.run||response.item||response;if(!sameSession(c))return;
        const pending=Object.values(state.chatPending).find(value=>value.runId===id);
        if(pending){pending.status=running(run)?'running':'finished';pending.error='';if(pending.threadId===state.threadId&&pending.projectId===(state.projectId||''))state.activeRun=run;}
        if(state.activeRun?.id===id){state.activeRun=run;renderMessages();renderConnection();}
        if(state.selectedRun?.id===id){state.selectedRun=run;renderRuns();}
        if(running(run))state.polling.set(id,setTimeout(poll,2000));
        else{state.polling.delete(id);await refresh();if(!sameSession(c))return;
          if(state.syncRun?.id===id){const target=state.syncRun.projectId,old=state.syncRun.before,now=feedbackSnapshot(target);const changed=now.filter(f=>old[f.id||f.feedback_id]!==feedbackVersion(f)).length;setText('sync-feedback-status',run.status==='succeeded'||run.status==='completed'?(state.errors.workspace&&state.projectId===target?'处理已完成，但反馈列表未刷新，请重试读取。':changed?'收到 '+changed+' 条新增或更新的反馈。':'暂无新反馈。'):'本次读取未完成，已有反馈保留。');errorAt('sync-feedback-error',run.error||'');state.syncRun=null;$('sync-feedback').disabled=!state.projectId;setText('sync-feedback','检查一次');}
        }
      }catch(e){state.polling.delete(id);if(sameSession(c)){const pending=Object.values(state.chatPending).find(value=>value.runId===id);if(pending){pending.status='uncertain';pending.error='暂时无法确认回复，已保留这条消息。';renderMessages();renderConnection();}else if(state.activeRun?.id===id){state.activeRun={...state.activeRun,status:'uncertain',error:'暂时无法确认回复，已保留你的消息。'};renderMessages();renderConnection();}if(state.syncRun?.id===id){errorAt('sync-feedback-error',e.message);} if(state.syncRun?.id===id){$('sync-feedback').disabled=false;setText('sync-feedback','检查读取结果');}}}
    };await poll();
  }
  async function startRun(path,body,c=context()){
    const data=await api(path,{method:'POST',body:path==='/chat'?{...body,project_id:c.projectId||'',thread_id:c.threadId}:{...body,project_id:c.projectId}});const id=data.run_id||data.id;if(!id)throw new APIError('操作结果尚未确认，请先检查已有结果，再继续原操作。',0,'write_uncertain');
    if(!sameSession(c))return id;if(path==='/chat'){const pending=state.chatPending[c.threadId];if(pending&&pending.id===body.operation_id){pending.runId=id;pending.status='running';pending.error='';}if(c.threadId===state.threadId&&String(c.projectId||'')===String(state.projectId||''))state.activeRun={id,status:'queued',kind:'chat',trigger:'manual',project_id:c.projectId||'',thread_id:c.threadId};renderMessages();renderConnection();trackRun(id);}return id;
  }
  const memberOptions=(ids=[],projectOnly=false,projectId=state.taskTargetId||state.projectId)=>{
    const allowed=list(state.projects.find(p=>p.id===projectId)?.member_ids);
    return allUsers().filter(u=>!projectOnly||allowed.includes(u.id)||ids.includes(u.id)).map(u=>'<label class="check-label"><input type="checkbox" value="'+esc(u.id)+'"'+(ids.includes(u.id)?' checked':'')+' />'+esc(displayName(u))+(u.identity_mode==='role_test'?'（角色测试）':'')+'</label>').join('');
  };
  function taskMembers(ids=[]){
    $('task-members').innerHTML=state.taskTargetId?memberOptions(ids,true,state.taskTargetId):'';
    show('task-member-hint',!state.taskTargetId);
  }
  function taskFields(){return {task_id:$('task-id').value.trim(),instructions:$('task-instructions').value.trim(),member_ids:[...$('task-members').querySelectorAll('input:checked')].map(i=>i.value),task_kind:$('task-kind').value,stage:$('task-kind').value==='onboarding'?$('task-stage').value:'',nature:$('task-nature').value,related_task:$('task-related').value.trim(),...(state.taskMode==='draft'?{reason:$('task-reason').value.trim()}:{} )};}
  function taskChanged(fields){return ['task_id','instructions','task_kind','stage','nature','related_task','reason'].some(k=>(state.task?.[k]||(k==='task_kind'?'work':''))!==fields[k])||JSON.stringify(list(state.task?.member_ids).slice().sort())!==JSON.stringify(fields.member_ids.slice().sort());}
  function taskKind(){const on=$('task-kind').value==='onboarding';show('task-stage-label',on);const option=[...$('task-nature').options].find(o=>o.value==='每次接入执行');option.disabled=!on;if(!on&&$('task-nature').value==='每次接入执行')$('task-nature').value='一次性';}
  function lockTask(locked,reasonLocked=locked){['task-id','task-instructions','task-related'].forEach(id=>$(id).readOnly=locked||(id==='task-id'&&!!state.task));$('task-reason').readOnly=reasonLocked;['task-kind','task-stage','task-nature'].forEach(id=>$(id).disabled=locked);$('task-members').querySelectorAll('input').forEach(i=>i.disabled=locked);}
  const recoveryKey=(kind,target='')=>[labStoragePrefix(),'rdos-pending-write',state.user?.id,state.projectId,kind,target].join(':');
  function saveRecovery(kind,target,value){try{sessionStorage.setItem(recoveryKey(kind,target),JSON.stringify(value));}catch{}}
  function getRecovery(kind,target=''){try{return JSON.parse(sessionStorage.getItem(recoveryKey(kind,target))||'null');}catch{return null;}}
  function clearRecovery(kind,target=''){try{sessionStorage.removeItem(recoveryKey(kind,target));}catch{}}
  const projectFields=()=>({name:$('project-form-name').value.trim(),description:$('project-form-description').value.trim(),node_ids:[...$('project-members').querySelectorAll('input:checked')].map(i=>i.value),folder_url:$('project-folder-url').value.trim()});
  const projectFieldsKey=fields=>JSON.stringify([fields.name,fields.description,fields.node_ids,fields.folder_url]);
  function showTask(task=null,mode='manual'){
    state.task=task;state.taskMode=mode;state.taskConflict=false;state.taskOperation=task?.publication_operation_id||uid();state.taskPending=null;state.taskPayloadKey=null;$('task-form').reset();errorAt('task-form-error','');setText('task-form-status','');
    state.taskTargetId=task?.project_id||state.projectId||'';
    const chooseProject=!state.taskTargetId;
    setText('task-dialog-title',mode==='draft'?'审阅'+draftActionLabel(task):task?'编辑任务':'新建任务');setText('task-current-project',chooseProject?'':'发布到：'+projectName(state.taskTargetId));
    show('task-project-label',chooseProject);put('task-project','<option value="">请选择项目</option>'+state.projects.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+'</option>').join(''));$('task-project').value=state.taskTargetId;
    const action=mode==='draft'?(task?.draft_action||'create'):'',withdrawDraft=action==='withdraw';
    setText('task-dialog-description',mode==='draft'?(withdrawDraft?'确认后停用本地测试后端中的这项任务；历史保留。':'确认后只写入本地测试任务表，不派发真实任务。'):task?'将更新任务要求并生成新版本。':'只写入本地测试任务表。');
    show('task-draft-impact',mode==='draft');put('task-draft-impact',mode==='draft'?'<p><b>'+esc(draftActionLabel(task))+'</b>'+(task?.base_task_version?' · 基于原版本 '+esc(task.base_task_version):'')+'</p><p>目标：'+esc(task?.task_id||'新任务')+'</p>':'');
    show('task-reason-label',mode==='draft');$('task-reason').value=task?.reason||'';
    $('task-id').value=task?.task_id||'';$('task-instructions').value=task?.instructions||'';$('task-related').value=task?.related_task||'';$('task-kind').value=task?.task_kind||'work';$('task-stage').value=task?.stage||'第一阶段';$('task-nature').value=task?.nature||'一次性';taskMembers(list(task?.member_ids));taskKind();
    const recovering=mode==='draft'&&!!task?.publication_operation_id;lockTask(recovering||withdrawDraft,recovering);setText('task-submit',recovering?'检查发布结果':mode==='draft'&&withdrawDraft?'确认停用':mode==='draft'&&action==='update'?'发布更新':task&&mode==='manual'?'发布更新':'发布任务');show('task-reject',mode==='draft'&&!recovering&&task?.status!=='published');
    if(recovering)setText('task-dialog-description','请先检查结果，再继续原发布。');
    const saved=task?.display_redacted?null:getRecovery('task',task?.id||task?.task_id||'');
    if(saved){
      state.taskPending=saved;state.taskOperation=saved.body.operation_id;const f=mode==='draft'?saved.fields:saved.body;state.taskTargetId=f?.project_id||state.taskTargetId;
      if(state.taskTargetId){show('task-project-label',false);$('task-project').value=state.taskTargetId;setText('task-current-project','发布到：'+projectName(state.taskTargetId));}
      for(const [key,id] of Object.entries({instructions:'task-instructions',task_id:'task-id',related_task:'task-related',task_kind:'task-kind',stage:'task-stage',nature:'task-nature',reason:'task-reason'}))if(f?.[key]!==undefined)$(id).value=f[key];
      taskMembers(list(f?.member_ids));lockTask(true);setText('task-submit','继续原发布');setText('task-form-status','已恢复原内容，继续前将核对发布结果。');
    }
    $('task-submit').disabled=!!task?.display_redacted;
    if(task?.display_redacted){lockTask(true);show('task-reject',false);setText('task-dialog-description','此草稿含已隐藏内容，本页仅供查看；请重新准备不含敏感信息的草稿。');}
    rememberForm('task-dialog');open('task-dialog');
  }
  function showUser(user=null){
    state.editUser=user;$('user-form').reset();errorAt('user-form-error','');setText('user-form-status','');setText('user-dialog-title',user?'编辑成员':'新增成员');$('user-username').value=user?.username||'';$('user-username').disabled=false;$('user-display').value=user?.display_name||'';$('user-admin').checked=list(user?.roles).includes('admin');$('user-researcher').checked=user?list(user.roles).includes('researcher'):true;$('user-identity').value=user?.identity_mode||'actual';show('user-password-label',!user);show('user-node-note',!user);rememberForm('user-dialog');open('user-dialog');
  }
  function projectNodeOptions(ids=[]){
    return registeredNodes().map(node=>'<label class="check-label"><input type="checkbox" value="'+esc(phase3NodeId(node))+'"'+(ids.includes(phase3NodeId(node))?' checked':'')+' />'+esc(node.display_name||node.label||phase3NodeId(node))+'</label>').join('')||'<p class="small-note">尚无登记节点，请先在设置中核对节点接入。</p>';
  }
  function projectFolderHint(){
    const custom=$('project-folder-url').value.trim();
    if(state.editProject?.is_default){setText('project-folder-hint','Default 项目提供共用的默认位置，本轮保留该文件夹。');return;}
    put('project-folder-hint',custom?'保存后使用指定的项目工作文件夹。':('未指定时使用 Default 的工作文件夹。 '+projectFolderLink(state.defaultFolderUrl,'查看默认位置 ↗')));
  }
  function showProject(project=null){
    state.editProject=project;state.projectOperation=uid();state.projectPayloadKey=null;state.projectPending=null;$('project-form').reset();$('project-form').querySelectorAll('input,textarea').forEach(i=>{i.readOnly=false;i.disabled=false;});errorAt('project-form-error','');setText('project-form-status','');setText('project-dialog-title',project?'编辑项目':'创建项目');$('project-form-name').value=project?.name||'';$('project-form-description').value=project?.description||'';$('project-folder-url').value=project?.folder_url||'';$('project-folder-url').required=false;$('project-members').innerHTML=projectNodeOptions(list(project?.node_ids));setText('save-project',project?'保存项目':'创建项目');
    const saved=getRecovery('project',project?.id||'');
    if(saved?.body){
      state.projectPending=saved;state.projectOperation=saved.body.operation_id;state.projectPayloadKey=projectFieldsKey(saved.body);
      $('project-form-name').value=saved.body.name;$('project-form-description').value=saved.body.description;$('project-folder-url').value=saved.body.folder_url;$('project-members').innerHTML=projectNodeOptions(saved.body.node_ids);
      $('project-form').querySelectorAll('input,textarea').forEach(i=>i.type==='checkbox'?i.disabled=true:i.readOnly=true);setText('save-project',project?.id?'检查保存结果':'检查创建结果');setText('project-form-status','上次保存结果尚未确认，将先核对原操作。');
    }
    if(project?.is_default)$('project-folder-url').readOnly=true;
    projectFolderHint();rememberForm('project-dialog');open('project-dialog');
  }
  function showPassword(user=null){
    state.passwordTarget=user;$('password-form').reset();if($('account-menu'))$('account-menu').open=false;errorAt('password-form-error','');
    setText('password-dialog-title',user?'重置成员密码':'修改我的密码');
    setText('password-target',user?'重置账号：'+displayName(user)+'（'+user.username+'）':'账号：'+(state.user?.username||displayName(state.user)));
    $('password-current-label').firstChild.textContent=user?'你的管理员密码':'当前密码';
    setText('password-session-note',user?'保存后，该成员需要使用新密码重新登录。请通过合适的私下方式告知对方。':'新密码至少 10 个字符。保存后，你需要重新登录。');
    rememberForm('password-dialog');open('password-dialog');
  }
  async function deactivateProject(id,button){
    await busy(button,async()=>{
      const c=context(),project=state.projects.find(p=>p.id===id);if(!project||project.is_default)return;
      const preview=await api('/projects/'+encodeURIComponent(id)+'/deactivation-preview');if(!current(c))return;
      const tasks=list(preview.tasks),known=state.projectActionOps[id]||getRecovery('project-deactivate',id)||preview.resume_operation;
      const operation=known?{operation_id:known.operation_id,expected_revision:known.expected_revision}:{operation_id:project.deactivation?.operation_id||uid(),expected_revision:project.deactivation?.expected_revision??preview.revision??project.revision};
      state.projectActionOps[id]=operation;
      const lines=tasks.map(t=>'• '+t.title+'（'+phase3Status(t)+'）');
      const message=(lines.length?'将暂停以下任务：\n'+lines.join('\n'):'没有需要新暂停的任务。')+'\n\n暂停后停用项目，保留成果和历史。节点下次检查后停止新增工作；重新启用项目不会自动恢复任务。';
      confirm(project.status==='deactivating'?'继续停用「'+project.name+'」？':'停用「'+project.name+'」？',message,project.status==='deactivating'?'继续处理':'暂停任务并停用',async()=>{
        saveRecovery('project-deactivate',id,operation);
        const response=await api('/projects/'+encodeURIComponent(id)+'/deactivate',{method:'POST',body:operation,timeout_ms:180000});if(!sameSession(c))return;
        const result=response.item||response;
        if(result.status==='inactive'){delete state.projectActionOps[id];clearRecovery('project-deactivate',id);}
        await refresh();toast(result.status==='inactive'?'项目已停用，历史记录保留。':'部分任务仍待处理，请从项目卡片继续停用。',result.status!=='inactive');
      });
    },'workspace-error');
  }
  function reactivateProject(id){
    const project=state.projects.find(p=>p.id===id),c=context();if(!project)return;
    const operation={operation_id:uid(),expected_revision:project.revision};
    confirm('重新启用「'+project.name+'」？','已暂停的任务继续保持暂停。需要时请在项目工作中逐项恢复。','重新启用',async()=>{
      await api('/projects/'+encodeURIComponent(id)+'/reactivate',{method:'POST',body:operation});if(!sameSession(c))return;
      await refresh();toast('项目已启用，任务状态保持原样。');
    });
  }

  function showProposal(item){
    state.proposal=item;state.publishOperation=item.publication_operation_id||uid();state.proposalEditing=false;
    if(state.proposalSourceChanged?.id!==item.id||state.proposalSourceChanged?.base_hash!==item.base_hash)state.proposalSourceChanged=null;
    errorAt('proposal-form-error',item.display_redacted?'此草稿含已隐藏内容，本页仅供查看；请重新准备不含敏感信息的草稿。':state.proposalSourceChanged?proposalStaleMessage(item):'');
    const editable=item.status!=='published'&&!item.publication_operation_id&&!item.display_redacted;
    delete $('proposal-review').dataset.rendered;put('proposal-review','<section class="proposal-purpose"><h3>修改目的</h3><div class="markdown-content">'+markdown(item.summary)+'</div>'+pill(item.status==='draft'?'待审阅':item.target_kind==='template'&&item.status==='published'?'已生效':item.status)+'</section><section class="proposal-target"><h3>'+(item.target_kind==='template'?'生效范围':'目标资料与影响')+'</h3><p>'+proposalTargetDisplay(item)+'</p><p>'+proposalImpact(item)+'</p></section><div id="proposal-preview" class="diff-grid"><section><h3>修改前</h3><div class="markdown-content">'+markdown(item.old_text||'追加新内容')+'</div></section><section><h3>修改后</h3><div class="markdown-content">'+markdown(item.new_text)+'</div></section></div><div id="proposal-editor" class="hidden stack-form"><label>修改目的<textarea aria-label="修改目的" id="proposal-summary" rows="3">'+esc(item.summary)+'</textarea></label><div class="diff-grid"><label>修改前<textarea aria-label="修改前" id="proposal-old" rows="9">'+esc(item.old_text)+'</textarea></label><label>修改后<textarea aria-label="修改后" id="proposal-new" rows="9">'+esc(item.new_text)+'</textarea></label></div><p id="proposal-unsaved" class="unsaved-notice"></p><button id="save-proposal" class="secondary" type="button">保存草稿</button></div><details><summary>查看依据</summary>'+evidenceContent(item.evidence)+'</details><details><summary>来源版本与记录标识</summary><p class="mono">'+esc(item.target_id)+'<br>'+esc(item.base_hash||'待核对')+'</p></details>'+(editable?'<div class="dialog-actions"><button id="reject-proposal" class="quiet danger" type="button">退回修改</button><button id="edit-proposal" class="secondary" type="button">编辑草稿</button><button id="publish-proposal" class="primary" type="button"'+(!item.draft_hash||item.status==='rejected'||state.proposalSourceChanged?' disabled':'')+'>确认发布</button></div>':'<div class="dialog-actions"><button class="secondary" data-close="proposal-dialog" type="button">关闭</button>'+(item.status!=='published'&&item.publication_operation_id&&!item.display_redacted?'<button id="publish-proposal" class="primary" type="button">检查发布结果</button>':'')+'</div>'));
    rememberForm('proposal-dialog');open('proposal-dialog');
  }
  function proposalChanged(){return state.proposal&&$('proposal-summary')&&['summary','old_text','new_text'].some(k=>$(k==='summary'?'proposal-summary':k==='old_text'?'proposal-old':'proposal-new').value!==(state.proposal[k]||''));}
  function updatePublishButton(){const changed=proposalChanged();setText('proposal-unsaved',changed?'有未保存修改，请保存后再发布。':'');if($('publish-proposal'))$('publish-proposal').disabled=changed||!state.proposal?.draft_hash||state.proposal.status==='rejected'||!!state.proposalSourceChanged;}
  function confirm(title,message,actionText,action,withReason=false){
    setText('confirm-title',title);setText('confirm-message',message);setText('confirm-action',actionText);show('confirm-reason-label',withReason);$('confirm-reason').value='';errorAt('confirm-error','');rememberForm('confirm-dialog');
    $('confirm-action').onclick=()=>formAction('confirm-dialog','confirm-error',async c=>{await action($('confirm-reason').value.trim(),c);if(sameSession(c))close('confirm-dialog',true);});open('confirm-dialog');
  }
  function uncertain(e){return ['write_uncertain','write_unverified','timeout','connection_failed'].includes(e.code)||e.status===504;}
  async function choosePath(path){
    const go=async()=>{const c=onboardingContext(),button=$(path==='new'?'choose-new':'choose-refresh');if(button.disabled||state.onboardingStarting)return;const seq=++state.onboardingStartSeq;state.onboardingRequestSeq++;state.onboardingStarting=true;button.disabled=true;button.setAttribute('aria-busy','true');
      try{const data=await api('/onboarding/start',{method:'POST',body:{path,restart:!!state.onboarding?.choice}});if(!currentOnboarding(c)||seq!==state.onboardingStartSeq)return;state.copied=false;state.lastCheck=0;state.instruction=null;state.instructionSeq++;state.instructionPromise=null;state.checking=false;state.onboarding=data;state.onboardingError='';state.onboardingCheckError='';state.instructionError='';state.reentering=false;renderOnboarding();await prepareInstruction(false);}
      catch(e){if(currentOnboarding(c)&&seq===state.onboardingStartSeq){state.onboardingError=e.message;renderOnboarding();}}
      finally{if(button.isConnected&&currentOnboarding(c)&&seq===state.onboardingStartSeq){state.onboardingStarting=false;button.disabled=false;button.removeAttribute('aria-busy');}}};
    if(state.onboarding?.choice)confirm('重新接入？','将开始新一轮接入，已有记录保留。','开始新一轮',go);else await go();
  }
  async function copyInstruction(kind){
    const c=kind==='project'?context():onboardingContext(),stillCurrent=()=>kind==='project'?current(c):currentOnboarding(c),i=kind==='project'?state.projectInstruction:state.instruction,errorId=kind==='project'?'project-instruction-error':'onboarding-action-error';
    errorAt(errorId,'');if(!i?.prompt){errorAt(errorId,'指令尚未生成，请先准备指令。');return;}if(expired(i)){errorAt(errorId,'指令已过期，请重新生成后复制。');return;}
    try{await navigator.clipboard.writeText(i.prompt);if(!stillCurrent()||(kind==='project'?state.projectInstruction:state.instruction)!==i)return;
      if(kind==='project'){i.copied=true;renderProjects();}else{try{sessionStorage.setItem(copyKey(),'yes');}catch{}state.copied=true;renderOnboarding();}
      toast('已复制，请粘贴到 Agent。');
    }catch{if(stillCurrent())errorAt(errorId,'复制失败，请展开“指令全文”手动复制。');}
  }
  // Phase 3 keeps business state and node receipts separate. Reads below use
  // cached Panel data; only the explicit sync action reads Feishu.
  const phase3Status = task => ({draft:'草稿',active:'已启动',pending_review:'结果待核对',paused:'已暂停',closed:'已结束'})[task?.business_status]||'待核对';
  const phase3TaskId = task => task?.task_id||task?.id||'';
  const phase3Tasks = () => list(phase3.data?.tasks);
  const phase3Drafts = () => list(phase3.data?.drafts);
  const phase3Documents = () => list(phase3.data?.documents);
  const phase3ArtifactDocuments = () => {
    const referenced=new Set(phase3Tasks().flatMap(task=>['input_refs','result_refs','artifact_refs','artifacts'].flatMap(key=>list(task[key])))
      .map(ref=>typeof ref==='string'?ref:ref?.document_id||ref?.object_id||ref?.id).filter(Boolean));
    return phase3Documents().filter(document=>!document.archived&&!document.retired
      &&(document.zone!=='agent'||referenced.has(document.id||document.object_id)));
  };
  const phase3TaskDocuments = () => phase3Documents().filter(d=>!d.archived&&!d.retired);
  const registeredNodes = () => (state.nodes.length?state.nodes:list(phase3.data?.nodes)).filter(n=>n.active!==false&&n.enabled!==false);
  const projectAllowsWork = () => !['inactive','deactivating'].includes((state.projects.find(p=>p.id===state.projectId)||phase3.data?.project)?.status);
  const phase3Nodes = () => {const ids=state.projects.find(p=>p.id===state.projectId)?.node_ids;return registeredNodes().filter(n=>!Array.isArray(ids)||ids.includes(phase3NodeId(n)));};
  const phase3NodeId = node => node?.public_id||node?.node_id||node?.id||'';
  const phase3NodeName = id => {const node=registeredNodes().find(n=>[n.public_id,n.node_id,n.id].includes(id));return node?.display_name||node?.label||id||'未指定节点';};
  const taskTypeName = value => ({action:'执行动作',deliverable:'制作成果',review:'执行动作'})[value]||'执行动作';
  const audienceName = value => ({admin:'管理员',project_members:'项目成员',researcher:'对应研发人员'})[value]||value;
  function phase3Links(){
    const found=new Set();
    const visit=value=>{if(typeof value==='string'&&/^https:\/\//.test(value))found.add(value);else if(Array.isArray(value))value.forEach(visit);else if(value&&typeof value==='object')Object.values(value).forEach(visit);};
    visit(phase3.data?.scope);phase3Documents().forEach(d=>visit(d.source_url));return found;
  }
  function phase3Link(value,title='打开飞书原文 ↗'){
    if(!phase3Links().has(value))return '';
    try{const u=new URL(value);if(u.protocol!=='https:'||u.username||u.password||u.port||!u.hostname.endsWith('.feishu.cn')||!/^\/(docx|base|drive\/folder)\//.test(u.pathname))return '';}
    catch{return '';}
    return '<a href="'+esc(value)+'" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">'+esc(title)+'</a>';
  }
  function phase3Query(){return '?project_id='+encodeURIComponent(state.projectId||state.projects.find(p=>p.is_default)?.id||'');}
  async function loadPhase3(){
    if(!state.user||phase3.loading||!state.projectId)return;
    const c=context(),seq=++phase3.sequence;phase3.loading=true;
    try{const data=await api((developerMode?'/phase3/developer':'/phase3/workspace')+phase3Query());if(!current(c)||seq!==phase3.sequence)return;phase3.data=data;phase3.projectId=c.projectId;phase3.error='';}
    catch(e){if(current(c)&&seq===phase3.sequence)phase3.error=e.message;}
    finally{if(current(c)&&seq===phase3.sequence){phase3.loading=false;renderPhase3();}}
  }
  function renderNodes(){if(admin())renderPeople();}

  function phase3Requirements(task){
    const refs=list(task.input_refs).map(ref=>{const d=phase3Documents().find(d=>d.id===ref.document_id||d.object_id===ref.document_id);return '<li>'+esc(d?.title||'已登记资料')+' · 版本 '+esc(ref.revision)+' '+phase3Link(d?.source_url)+'</li>';}).join('');
    const completion={receipt_recorded:'有效完成结果写回并核对后结束',artifact_present:'指定产物与版本核验后结束',source_versions_match:'指定输入资料版本核验后结束'}[task.completion_rule?.kind]||'有效完成结果写回并核对后结束';
    return '<dl class="phase3-meta"><dt>负责人</dt><dd>'+esc(task.owner_name||memberName(task.owner_id))+'</dd><dt>执行节点</dt><dd>'+list(task.executor_node_ids).map(id=>esc(phase3NodeName(id))).join('、')+'</dd><dt>任务类别</dt><dd>'+esc(taskTypeName(task.task_type))+'</dd><dt>目标与要求</dt><dd class="phase3-text">'+esc(task.goal)+'</dd><dt>交付要求</dt><dd class="phase3-text">'+esc(task.deliverable)+'</dd><dt>成果面向</dt><dd>'+list(task.deliverable_audience||['admin','project_members']).map(x=>esc(audienceName(x))).join('、')+'</dd><dt>下发／回传</dt><dd>'+list(task.dispatch_to).map(x=>x==='human'?'研发人员':'Agent').join('＋')+' ／ '+list(task.return_to).map(x=>x==='human'?'研发人员':'Agent').join('＋')+'</dd><dt>产物位置</dt><dd>'+(task.output_zone==='agent'?'Agent 协作区':'项目协作区')+'</dd><dt>完成方式</dt><dd>'+esc(completion)+'</dd></dl>'+(refs?'<h4>输入依据</h4><ul>'+refs+'</ul>':'')+(list(task.editable_document_ids).length?'<p class="small-note">允许局部修改：'+list(task.editable_document_ids).map(id=>esc(phase3Documents().find(d=>d.id===id||d.object_id===id)?.title||'已登记文档')).join('、')+'</p>':'')+(task.change_reason?'<p class="small-note">调整说明：'+esc(task.change_reason)+'</p>':'');
  }
  function phase3Receipts(task){
    const events=list(phase3.data?.events).filter(e=>e.task_id===phase3TaskId(task)||list(e.task_ids).includes(phase3TaskId(task))||list(e.task_refs).some(ref=>ref.task_id===phase3TaskId(task)));
    const receiptNames={read:'已读取',read_receipt:'已读取',started:'已开始',start_receipt:'已开始',submission:'已提交',submitted:'已提交',result:'已提交',closed_ack:'结束回执',paused_ack:'暂停回执',stop_receipt:'已停止',pause_receipt:'已暂停',review:'Agent 审阅',artifact:'产物提交',progress:'进展',blocker:'受阻',blocked:'受阻',check_result:'核对结果'};
    const actual=events.filter(e=>receiptNames[e.event_type||e.type]);
    const latest=actual.at(-1);
    if(!latest&&task.business_status==='closed'&&task.receipt_status!=='verified')return '';
    return latest?'<p class="small-note">最近节点动态：'+esc(receiptNames[latest.event_type||latest.type])+' · '+esc(phase3NodeName(latest.node_id||latest.claimed_node_id))+(latest.created_at||latest.at?' · '+date(latest.created_at||latest.at):task.execution_result?.verified_at?' · 核对时间 '+date(task.execution_result.verified_at):' · 时间未提供')+'</p>':'<p class="small-note">'+(task.receipt_status==='verified'?'执行回执已核对':'等待读取与执行结果')+'</p>';
  }
  function phase3Execution(task){
    if(task.business_status==='closed'&&task.close_reason==='manual_completed')return '<p class="small-note">管理员已标记完成。节点执行与回执保留实际记录。</p>';
    if(task.business_status==='closed'&&task.close_reason==='cancelled')return '<p class="small-note">已取消，停止派发。未将本任务标记为执行完成。</p>';
    const result=task.result_status||task.execution_result?.result_status;
    const status={completed:'动作完成',partial:'部分完成',blocked:'受阻'}[result]||'等待执行';
    const receipt=task.receipt_status==='verified'?'已写回并核对':task.submission_event_id?'已收到，待核对':'等待结果';
    const text=task.execution_result?.summary||task.summary||'';
    return '<div class="business-outcomes"><span>执行：'+esc(status)+'</span><span>回执：'+esc(receipt)+'</span></div>'+(text?'<p>'+esc(summary(text,220))+'</p>':'');
  }
  function renderCollaborationEvents(){
    const names={read:'读取回执',read_receipt:'读取回执',start:'开始工作',started:'开始工作',submission:'提交结果',review:'Agent 审阅',blocker:'问题反馈',blocked:'问题反馈',question:'澄清问题',clarification:'澄清记录',progress:'工作进展',correction:'更正记录',project_feedback:'项目反馈',paused_ack:'暂停回执',closed_ack:'结束回执',stop_ack:'停止回执',task_change:'任务更新',check_result:'核对结果'};
    const internalTypes=new Set(['node_registry_snapshot','peer_capability_ack','peer_enrollment_request']);
    const events=list(phase3.data?.events).filter(event=>!internalTypes.has(event.event_type||event.type)).slice().sort((a,b)=>String(b.created_at||b.at||'').localeCompare(String(a.created_at||a.at||'')));
    put('phase3-events',events.map(event=>{
      const type=event.event_type||event.type,node=event.claimed_node_id||event.node_id;
      const taskNames=list(event.task_refs).map(ref=>{const task=phase3Tasks().find(item=>phase3TaskId(item)===ref.task_id);return (task?.title||'关联任务')+(ref.requirements_version?' · 要求 v'+ref.requirements_version:'');});
      const knownStatus={completed:'节点报告执行完成。',partial:'节点报告部分完成，具体原因见协作原文。',blocked:'节点报告受阻，具体原因见协作原文。'}[event.result_status];
      const text=event.summary||knownStatus||'已收到'+(names[type]||'协作记录')+'。';
      const artifacts=list(event.artifact_refs).map(ref=>{const document=phase3Documents().find(item=>[item.id,item.object_id].includes(ref.document_id||ref.id));return '<p>'+esc(ref.title||document?.title||'已提交成果')+(ref.revision?' · 版本 '+esc(ref.revision):'')+' '+phase3Link(document?.source_url||ref.source_url,'打开成果 ↗')+'</p>';}).join('');
      return '<article class="item-card" data-key="collaboration-event:'+esc(event.id)+'"><div class="row"><h3>'+esc(names[type]||'协作记录')+'</h3>'+(event.late_or_inactive?pill('历史记录'):'')+'</div><p>'+esc(text)+'</p>'+(taskNames.length?'<p class="small-note">'+taskNames.map(esc).join('；')+'</p>':'')+'<p class="small-note">'+(node?'节点自报：'+esc(phase3NodeName(node)):'中央记录')+' · '+date(event.created_at||event.at)+'</p>'+(event.correction_of?'<p class="small-note">本条为对先前记录的更正；原记录保留。</p>':'')+artifacts+phase3Link(phase3.data?.scope?.event_table_url,'查看协作原文 ↗')+'</article>';
    }).join('')||empty(phase3.loading?'正在读取协作记录':'暂无协作记录'));
  }
  function renderPhase3(){
    if(!$('phase3-tasks'))return;
    renderNodes();put('phase3-project',state.projects.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+(p.status==='inactive'?'（已停用）':p.status==='deactivating'?'（停用处理中）':'')+'</option>').join(''));$('phase3-project').value=state.projectId||'';
    errorAt('phase3-error',phase3.error?(phase3.data?'更新失败，显示上次记录。':'')+phase3.error:'');
    const data=phase3.data;
    $('phase3-new').disabled=!data||!phase3Nodes().length||!projectAllowsWork();$('phase3-sync').disabled=phase3.loading||!!phase3.syncing||!data;
    const sync=state.syncResults[state.projectId],last=data?.last_refresh,lastSuccess=last?.at||(typeof last==='string'?last:null),syncError=sync?.error||(last?.complete===false?(last.error||'本次读取未完成，已有记录保留。'):''),syncIssue=last?.issue_count>0?(last.issue_summary||last.issue_count+' 项记录待核对；有效进展已同步。'):'';
    setText('phase3-sync-last',(lastSuccess?'上次成功同步：'+date(lastSuccess):'尚无成功同步记录。')+(syncError?' 最近一次同步未完成：'+syncError:'')+(syncIssue?' '+syncIssue:''));
    $('phase3-sync-last').classList.toggle('sync-warning',!!syncIssue||!!syncError);
    if(!data){put('phase3-summary','<span class="small-note">'+(phase3.loading?'正在读取协作记录':'协作记录尚未读取')+'</span>');renderCollaborationEvents();renderSidebar();return;}
    const tasks=phase3Tasks(),active=tasks.filter(t=>t.business_status!=='closed');
    const visibleTasks=navigation.showClosed?tasks:active;
    put('phase3-summary','<b>'+esc(data.project?.name||data.project?.title||'项目协作')+'</b><p>'+active.length+' 项当前任务</p>');
    const zones=data.scope?.zones||{};
    put('phase3-zones',[['project','项目协作','项目依据与成果'],['agent','Agent 协作','任务登记与协作记录']].map(([key,title,description])=>'<article class="panel"><h2>'+title+'</h2><p>'+description+'</p>'+phase3Link(zones[key]?.source_url,'打开协作文件夹 ↗')+(key==='agent'?'<div class="project-links">'+phase3Link(data.scope?.task_table_url,'任务登记 ↗')+' '+phase3Link(data.scope?.event_table_url,'协作记录 ↗')+'</div>':'')+'</article>').join(''));
    put('phase3-tasks',visibleTasks.map(task=>{
      const id=phase3TaskId(task),status=task.business_status,closed=status==='closed';
      const projectInactive=!projectAllowsWork();
      const buttons=!admin()||closed||task.integrity_error||projectInactive?'':(status==='paused'?'<button class="secondary" data-phase3-action="resume" data-phase3-id="'+esc(id)+'" type="button">恢复</button>':'<button class="quiet" data-phase3-action="pause" data-phase3-id="'+esc(id)+'" type="button">暂停</button>')+'<button class="quiet" data-phase3-edit="'+esc(id)+'" type="button">调整要求</button>'+'<button class="secondary" data-phase3-action="manual_complete" data-phase3-id="'+esc(id)+'" type="button">标记完成</button>'+'<button class="quiet danger" data-phase3-action="cancel" data-phase3-id="'+esc(id)+'" type="button">取消任务</button>';
      const results=list(task.result_refs).map(ref=>{const d=phase3Documents().find(d=>d.id===ref.document_id||d.object_id===ref.document_id);return '<p>成果：'+esc(d?.title||'已提交成果')+' · v'+esc(ref.revision)+' '+phase3Link(ref.source_url||d?.source_url)+'</p>';}).join('');
      return '<article class="item-card" data-key="phase3-task:'+esc(id)+'"><div class="row"><h3>'+esc(task.title)+'</h3>'+pill(phase3Status(task),status==='pending_review'?'warn':'')+'</div><div class="badge-row">'+pill('要求 v'+task.requirements_version)+pill(taskTypeName(task.task_type))+'</div>'+(task.integrity_error?'<p class="form-error" role="alert">任务记录存在冲突，已暂时停用操作。请联系管理员核对。</p>':'')+(task.input_refs_stale?'<p class="notice" role="status">输入资料已有变化，请核对是否需要调整任务。</p>':'')+phase3Execution(task)+results+'<details><summary>当前要求与依据</summary>'+phase3Requirements(task)+'</details>'+phase3Receipts(task)+(closed?'<p class="small-note">结束原因：'+esc(({completed:'完成',manual_completed:'管理员标记完成',cancelled:'取消',replaced:'被替代',superseded:'被替代'})[task.close_reason]||task.closure_reason||'已结束')+'</p>':'')+(task.summary_stale?'<p class="form-error">执行说明或摘要需要刷新。</p>':'')+'<div class="item-actions">'+buttons+'<button class="quiet" data-phase3-history="'+esc(id)+'" type="button">版本与历史</button></div></article>';
    }).join('')||empty(navigation.showClosed?'暂无已生效任务':'暂无进行中的任务',!navigation.showClosed&&tasks.length?'勾选“显示已关闭任务”可查看历史任务。':''));
    put('phase3-drafts',phase3Drafts().filter(d=>d.status==='draft').map(d=>'<article class="item-card" data-key="phase3-draft:'+esc(d.id)+'"><div class="row"><h3>'+esc(d.payload?.title||d.title||'任务草稿')+'</h3>'+pill('尚未生效','warn')+'</div>'+phase3Requirements(d.payload||d)+'<p class="small-note">'+(d.base_requirements_version?'基于要求 v'+esc(d.base_requirements_version):'新任务草稿')+'</p><div class="item-actions"><button class="quiet" data-phase3-edit-draft="'+esc(d.id)+'" type="button"'+(!projectAllowsWork()?' disabled':'')+'>编辑草稿</button><button class="primary" data-phase3-draft="'+esc(d.id)+'" type="button"'+(!projectAllowsWork()?' disabled':'')+'>'+(d.base_requirements_version?'确认调整':'确认并开始')+'</button>'+draftControls('phase3',d)+'</div></article>').join('')||empty('暂无待确认草稿'));
    put('phase3-drafts',$('phase3-drafts').innerHTML+deletedDrafts('phase3',[...phase3Drafts(),...list(data.deleted_drafts)]));
    put('phase3-documents',phase3ArtifactDocuments().map(d=>'<article class="item-card"><h3>'+esc(d.title||'项目文档')+'</h3><p class="small-note">版本 '+esc(d.revision??'待核对')+'</p>'+phase3Link(d.source_url)+'</article>').join('')||empty('尚无项目成果'));
    const decisions=[];
    tasks.filter(t=>t.result_status==='blocked'||t.result_status==='partial').forEach(t=>decisions.push('<p>「'+esc(t.title)+'」'+(t.result_status==='blocked'?'执行受阻':'部分完成')+'：'+esc(summary(t.execution_result?.summary||'请查看任务中的当前结果。',160))+'</p>'));
    list(data.operations).filter(o=>['failed','partial','uncertain','pending'].includes(o.status)).forEach(o=>decisions.push('<p class="form-error">'+esc(o.summary||'有一项操作尚未核对完成，请先检查已有结果。')+'</p>'));
    if(developerMode)decisions.push('<details><summary>开发原始记录</summary><pre>'+esc(safeJSON({quarantine:data.quarantine,operations:data.operations,events:data.events}))+'</pre></details>');
    put('phase3-decisions',decisions.join('')||'<p class="muted">当前没有需要你处理的事项。</p>');
    renderCollaborationEvents();renderSidebar();
  }

  function showPhase3Task(task=null,draft=null){
    if(!admin())return;
    if(!projectAllowsWork()){errorAt('phase3-error','项目已停用或正在停用，不能起草或发布任务。历史记录仍可查看。');return;}
    phase3.task=task;phase3.draft=draft;phase3.taskOperation=uid();
    const p=draft?.payload||task||{};
    $('phase3-task-form').reset();
    setText('phase3-task-heading',draft?'编辑协作任务草稿':task?'起草任务调整':'起草协作任务');
    setText('phase3-task-baseline',task?'基于要求 v'+task.requirements_version+'／记录修订 '+task.record_revision+'；保存不会立即改变有效要求。':'草稿保存后仍需明确开始；发布不代表节点已读取。');
    const changedInputs=list(p.input_refs).some(ref=>{const current=phase3Documents().find(d=>d.id===ref.document_id||d.object_id===ref.document_id);return current&&(String(current.revision)!==String(ref.revision)||current.content_hash!==ref.content_hash);});
    if(changedInputs)setText('phase3-task-baseline',$('phase3-task-baseline').textContent+' 输入资料已有新版本；保存的草稿将引用当前选中的资料版本，请审阅差异。');
    $('phase3-task-title').value=p.title||'';$('phase3-task-goal').value=p.goal||'';$('phase3-task-deliverable').value=p.deliverable||'';
    put('phase3-task-node',phase3Nodes().map(node=>'<option value="'+esc(phase3NodeId(node))+'">'+esc(node.display_name||node.label||phase3NodeId(node))+'</option>').join(''));
    const assigned=phase3Nodes().find(node=>list(p.executor_node_ids).some(id=>[node.public_id,node.node_id,node.id].includes(id)));
    $('phase3-task-node').value=phase3NodeId(assigned||phase3Nodes()[0]);$('phase3-task-zone').value=p.output_zone||'project';
    $('phase3-task-dispatch').value=list(p.dispatch_to).includes('human')?'both':'agent';$('phase3-task-return').value=list(p.return_to).length>1?'both':list(p.return_to)[0]||'both';
    $('phase3-task-completion').value=['artifact_present','source_versions_match'].includes(p.completion_rule?.kind)?p.completion_rule.kind:'receipt_recorded';
    $('phase3-task-type').value=p.task_type==='deliverable'?'deliverable':'action';$('phase3-task-audience').value=list(p.deliverable_audience||['admin','project_members']).join(',');
    put('phase3-task-input',phase3TaskDocuments().map(d=>'<option value="'+esc(d.id||d.object_id)+'">'+esc(d.title||d.id)+' · v'+esc(d.revision)+'</option>').join(''));
    [...$('phase3-task-input').options].forEach(o=>o.selected=list(p.input_refs).some(ref=>ref.document_id===o.value));$('phase3-task-change').value=p.change_reason||'';
    put('phase3-task-editable',phase3TaskDocuments().map(d=>'<option value="'+esc(d.id||d.object_id)+'">'+esc(d.title||d.id)+'</option>').join(''));
    [...$('phase3-task-editable').options].forEach(o=>o.selected=list(p.editable_document_ids).includes(o.value));
    show('phase3-task-change-label',!!task);errorAt('phase3-task-form-error','');rememberForm('phase3-task-dialog');open('phase3-task-dialog');
  }
  function showPhase3Action(task,action,draft=null){
    if(!admin()||!task)return;
    if(!projectAllowsWork()){errorAt('phase3-error','项目已停用或正在停用，任务暂时不能变更。');return;}
    if(draft?.display_redacted){errorAt('phase3-error','草稿含已隐藏内容，请清理后重新保存。');return;}
    phase3.action={task,action,draft,operation_id:uid()};$('phase3-action-form').reset();
    const title={start:'确认并开始任务',update:'确认任务调整',pause:'暂停任务',resume:'恢复当前版本',manual_complete:'标记完成',cancel:'取消任务'}[action]||'核对任务动作';
    setText('phase3-action-heading',title);setText('phase3-action-submit',title);
    const payload=draft?.payload||task;
    const periodic=!!task.periodicity||task.nature==='周期';
    const impact=action==='manual_complete'?'将以管理员操作结束'+(periodic?'整个周期任务，后续不再产生本任务的新周期。':'这项任务。')+'节点执行和回执保留实际记录；共用定时安排不变。':action==='cancel'?'取消后保留产物和历史，停止新增派发；节点本地停止情况按实际回执记录。':action==='pause'?'暂停后等待节点下次检查；不代表节点已经停止。':'按当前有效要求恢复。';
    put('phase3-action-preview','<h3>'+esc(payload.title)+'</h3><p class="small-note">'+(draft?'草稿基于要求 v'+esc(draft.base_requirements_version||0)+'／记录修订 '+esc(draft.base_revision||0):'要求 v'+esc(task.requirements_version)+'／记录修订 '+esc(task.record_revision))+'</p>'+(['start','update'].includes(action)?phase3Requirements(payload):'<p>'+esc(impact)+'</p>'));
    show('phase3-action-reason-label',!['start','update'].includes(action));$('phase3-action-reason').required=action==='cancel';
    errorAt('phase3-action-error','');rememberForm('phase3-action-dialog');open('phase3-action-dialog');
  }
  async function showPhase3History(id,button){
    await busy(button,async()=>{const c=context();const data=await api('/phase3/tasks/'+encodeURIComponent(id)+'/history'+phase3Query());if(!current(c))return;const items=Array.isArray(data.items)?data.items:[...list(data.versions),...list(data.events),...(developerMode?list(data.operations):[])];put('phase3-history',items.length?items.map(item=>'<article class="item-card"><h3>'+esc(item.title||({submission:'执行结果',review:'执行动作',task_change:'要求调整',read_receipt:'已读取',progress:'进展',blocker:'受阻',correction:'更正'}[item.event_type])||'历史记录')+'</h3><p class="small-note">要求 v'+esc(item.requirements_version??item.version??'—')+' · '+date(item.created_at||item.at||item.updated_at)+'</p>'+(item.payload?.goal?'<p class="phase3-text">'+esc(item.payload.goal)+'</p>':'')+(item.summary||item.execution_result?.summary?'<p>'+esc(summary(item.summary||item.execution_result.summary,240))+'</p>':'')+(item.change_reason?'<p>'+esc(item.change_reason)+'</p>':'')+(developerMode?'<details><summary>原始记录</summary><pre>'+esc(safeJSON(item))+'</pre></details>':'')+'</article>').join(''):empty('暂无历史记录'));open('phase3-history-dialog');},'phase3-error');
  }

  $('phase3-project').addEventListener('change',()=>selectProject($('phase3-project').value,'collaboration'));
  $('phase3-sync').addEventListener('click',()=>busy($('phase3-sync'),async()=>{
    const c=context(),query=phase3Query(),key=c.projectId||'';state.phase3SyncOperations=state.phase3SyncOperations||{};
    state.phase3SyncOperations[key]=state.phase3SyncOperations[key]||uid();phase3.syncing=true;setText('phase3-sync-status','正在同步当前项目的飞书进展…');
    try{
      await api('/phase3/sync'+query,{method:'POST',body:{operation_id:state.phase3SyncOperations[key]},timeout_ms:180000});
      if(!sameSession(c))return;delete state.phase3SyncOperations[key];state.syncResults[key]={at:new Date().toISOString()};
      if(!current(c))return;await loadPhase3();const last=phase3.data?.last_refresh;setText('phase3-sync-status',last?.issue_count>0?(last.issue_summary||last.issue_count+' 项记录待核对；有效进展已同步。'):'已同步当前项目的任务和执行结果。');
    }catch(error){if(sameSession(c))state.syncResults[key]={error:error.message};if(current(c))setText('phase3-sync-status','同步未完成，已有记录保留。');throw error;}
    finally{if(sameSession(c)){phase3.syncing=false;renderPhase3();}}
  },'phase3-error'));

  $('phase3-new').addEventListener('click',()=>showPhase3Task());
  $('phase3-task-form').addEventListener('submit',e=>{e.preventDefault();formAction('phase3-task-dialog','phase3-task-form-error',async c=>{
    const selections=[...$('phase3-task-input').selectedOptions].map(o=>o.value),selected=phase3Documents().filter(d=>selections.includes(d.id||d.object_id)),task=phase3.task,draft=phase3.draft;
    const audience=value=>value==='both'?['agent','human']:[value];
    const payload={task_type:$('phase3-task-type').value,deliverable_audience:$('phase3-task-audience').value.split(','),title:$('phase3-task-title').value.trim(),goal:$('phase3-task-goal').value.trim(),deliverable:$('phase3-task-deliverable').value.trim(),owner_id:(draft?.payload||task)?.owner_id||phase3.data?.project?.owner_id||state.user.id,executor_node_ids:[$('phase3-task-node').value],input_refs:selected.map(d=>({document_id:d.id||d.object_id,revision:d.revision,content_hash:d.content_hash})),output_zone:$('phase3-task-zone').value,dispatch_to:audience($('phase3-task-dispatch').value),return_to:audience($('phase3-task-return').value),completion_rule:{kind:$('phase3-task-completion').value},due_at:(draft?.payload||task)?.due_at||''};
    payload.editable_document_ids=[...$('phase3-task-editable').selectedOptions].map(o=>o.value);
    if(payload.completion_rule.kind==='source_versions_match'&&!payload.input_refs.length)throw new APIError('资料版本核对任务需要先选择具体输入资料及版本。');
    if(task){payload.task_id=phase3TaskId(task);payload.change_reason=$('phase3-task-change').value.trim();if(!payload.change_reason)throw new APIError('请说明调整原因、旧成果如何处理，以及是否需要重新执行。');}
    const path=draft?'/phase3/drafts/'+encodeURIComponent(draft.id)+'/edit':'/phase3/drafts';
    await api(path+phase3Query(),{method:'POST',body:draft?{payload,expected_hash:draft.draft_hash}:{payload,operation_id:phase3.taskOperation},timeout_ms:180000});if(!sameSession(c))return;close('phase3-task-dialog',true);await loadPhase3();toast('草稿已保存，请在待审阅草稿中核对完整内容后开始或应用调整。');
  });});
  $('phase3-action-form').addEventListener('submit',e=>{e.preventDefault();formAction('phase3-action-dialog','phase3-action-error',async c=>{
    const {task,action,draft,operation_id}=phase3.action;
    const payload=draft?{draft_id:draft.id,expected_hash:draft.draft_hash}:{reason:$('phase3-action-reason').value.trim()};
    if(action==='manual_complete'){payload.note=payload.reason;delete payload.reason;}
    await api('/phase3/tasks/'+encodeURIComponent(draft?.task_id||phase3TaskId(task))+'/actions/'+action+phase3Query(),{method:'POST',body:{operation_id,expected_revision:draft?Number(draft.base_revision||0):task.record_revision,expected_requirements_version:draft?Number(draft.base_requirements_version||0):task.requirements_version,payload},timeout_ms:180000});
    if(!sameSession(c))return;close('phase3-action-dialog',true);await loadPhase3();toast(action==='start'?'任务已生效；节点是否读取与执行等待真实回执。':action==='update'?'新要求已生效；节点是否改用新版等待读取回执。':action==='cancel'?'任务已取消；本地停止情况仍按实际节点回执核对。':'任务动作已记录并核对。');
  });});

  $('login-form').addEventListener('submit',async e=>{
    e.preventDefault();const b=e.submitter;if(b.disabled)return;b.disabled=true;b.setAttribute('aria-busy','true');b.textContent='正在登录…';setText('login-error','');
    try{const data=await api('/auth/login',{method:'POST',body:{username:$('login-username').value.trim(),password:$('login-password').value}});$('login-password').value='';beginSession(data.user||data);await refresh();}
    catch(error){setText('login-error',error.message);}finally{b.disabled=false;b.removeAttribute('aria-busy');b.textContent='登录';}
  });
  $('logout').addEventListener('click',()=>busy($('logout'),async()=>{await api('/auth/logout',{method:'POST',body:{}});resetSession();}));
  $('work-show-all').addEventListener('click',()=>{clearProject();state.projectId=null;state.page='work';render();refresh();});
  $('open-drawer').addEventListener('click',()=>{if(!developerMode)return;state.selectedRun=null;renderRuns();open('workspace-drawer');loadRuns();});
  $('load-more-runs').addEventListener('click',()=>loadRuns(true));
  $('choose-new').addEventListener('click',()=>choosePath('new'));$('choose-refresh').addEventListener('click',()=>choosePath('refresh'));
  $('change-onboarding-path').addEventListener('click',()=>{state.reentering=true;renderOnboarding();});
  $('prepare-onboarding-instruction').addEventListener('click',requestOnboardingInstruction);
  $('continue-onboarding').addEventListener('click',requestOnboardingInstruction);
  $('check-onboarding').addEventListener('click',()=>checkOnboarding(true));$('copy-onboarding-instruction').addEventListener('click',()=>expired(state.instruction)?requestOnboardingInstruction():copyInstruction('onboarding'));
  $('onboarding-go-project').addEventListener('click',()=>{state.page='projects';render();});
  $('open-setup').addEventListener('click',()=>busy($('open-setup'),async()=>{const c=context(),data=await api('/onboarding/setup',{text:true});if(!current(c))return;put('setup-content',markdown(data));open('setup-dialog');},'onboarding-action-error'));
  $('new-project').addEventListener('click',()=>showProject());
  $('project-folder-url').addEventListener('input',projectFolderHint);
  $('show-inactive-projects').addEventListener('change',()=>{state.showInactive=$('show-inactive-projects').checked;renderProjects();});
  $('phase3-show-closed')?.addEventListener('change',()=>{navigation.showClosed=$('phase3-show-closed').checked;renderPhase3();});
  $('project-form').addEventListener('submit',async e=>{
    e.preventDefault();if(state.forms.get('project-dialog')?.busy)return;
    const target=state.editProject,targetId=target?.id||'',fields=projectFields();if(!fields.node_ids.length){errorAt('project-form-error','请至少选择一个参与节点。');return;}
    const inputKey=projectFieldsKey(fields);
    if(state.projectPayloadKey&&state.projectPayloadKey!==inputKey)state.projectOperation=uid();
    state.projectPayloadKey=inputKey;
    const body=state.projectPending?.body||{...fields,operation_id:state.projectOperation,...(targetId?{expected_revision:target.revision}:{})};
    await formAction('project-dialog','project-form-error',async c=>{
      setText('project-form-status','正在保存项目…');saveRecovery('project',targetId,{body});
      try{
        await api(targetId?'/projects/'+encodeURIComponent(targetId):'/projects',{method:targetId?'PATCH':'POST',body,timeout_ms:180000});
        if(!sameSession(c))return;clearRecovery('project',targetId);state.projectPending=null;close('project-dialog',true);await refresh();toast('项目已保存。');
      }catch(error){
        if(!sameSession(c))return;
        if(uncertain(error)){state.projectPending={body};setText('project-form-status','结果尚未确认。再次提交将核对原操作，不重复创建。');$('project-form').querySelectorAll('input,textarea').forEach(i=>i.type==='checkbox'?i.disabled=true:i.readOnly=true);setText('save-project',targetId?'检查保存结果':'检查创建结果');}
        else{clearRecovery('project',targetId);state.projectPending=null;setText('project-form-status','输入已保留，请核对后再保存。');}
        throw error;
      }
    });
  });

  $('copy-project-instruction').addEventListener('click',()=>{const i=state.projectInstruction;if(i&&(expired(i)||i.error||!i.prompt))prepareProject(i.project_id);else copyInstruction('project');});
  $('chat-project').addEventListener('change',async()=>{
    const id=$('chat-project').value;
    if(id)await selectProject(id,'agent');
    else{clearProject();state.projectId=null;state.page='agent';render();await refresh();}
  });
  $('thread-select').addEventListener('change',async()=>{
    if(state.threadId)state.chatDrafts[state.threadId]=$('chat-message').value;
    state.threadId=$('thread-select').value||null;state.threadSelections[state.projectId||'']=state.threadId;
    state.generation++;state.refreshPromise=null;state.activeRun=null;state.chatOperation=null;state.chatViewed=null;state.chatComposing=false;render();await refresh();
  });
  $('new-thread').addEventListener('click',()=>{
    if(!admin())return;
    $('thread-form').reset();errorAt('thread-form-error','');rememberForm('thread-dialog');open('thread-dialog');$('thread-title').focus();
  });
  $('thread-form')?.addEventListener('submit',async e=>{
    e.preventDefault();const title=$('thread-title').value.trim();
    if(!title){errorAt('thread-form-error','请填写对话主题。');$('thread-title').focus();return;}
    if(title.length>80){errorAt('thread-form-error','对话主题最多 80 个字符。');return;}
    await formAction('thread-dialog','thread-form-error',async c=>{
      if(!admin()||!current(c))throw new APIError('项目范围或当前对话已变化，请关闭后重新创建。');
      const response=await api('/threads',{method:'POST',body:{project_id:c.projectId||'',title}});if(!current(c))return;
      if(state.threadId)state.chatDrafts[state.threadId]=$('chat-message').value;
      state.threads.unshift(response.item);state.threadId=response.item.id;state.threadSelections[state.projectId||'']=state.threadId;
      state.generation++;state.refreshPromise=null;state.activeRun=null;state.chatOperation=null;state.chatViewed=null;state.chatComposing=false;state.page='agent';close('thread-dialog',true);render();await refresh();if(state.user&&state.page==='agent')$('chat-message').focus();
    });
  });
  async function sendChat(retry=null){
    const raw=retry?.raw??$('chat-message').value,message=raw.trim(),c=context(),pending=pendingChat(),thread=state.threads.find(value=>value.id===c.threadId);
    if(!message||!admin()||!thread||thread.readonly||(!retry&&$('chat-send').disabled)||retry&&['submitting','running'].includes(retry.status))return;
    if(!retry&&pending?.error&&pending.message!==message){toast('请先继续上一条消息，确认结果后再发送新内容。',true);return;}
    const operation=retry||(pending?.message===message&&pending.error?pending:{message,raw,id:uid(),threadId:c.threadId,projectId:c.projectId||'',created_at:new Date().toISOString()});
    if(operation.threadId!==c.threadId||operation.projectId!==(c.projectId||''))return;
    operation.status='submitting';operation.error='';state.chatPending[c.threadId]=operation;
    errorAt('chat-error','');setText('chat-status','');
    if($('chat-message').value===raw)$('chat-message').value='';
    state.chatDrafts[c.threadId]=$('chat-message').value;renderConnection();renderMessages();
    try{
      await startRun('/chat',{message,operation_id:operation.id},c);
      if(!sameSession(c))return;
      if(current(c))await refresh();
    }catch(error){
      if(!sameSession(c))return;
      operation.status='uncertain';operation.error=error.message;
      if(current(c)){if(!$('chat-message').value){$('chat-message').value=raw;state.chatDrafts[c.threadId]=raw;}renderConnection();renderMessages();}
    }
  }
  $('chat-form').addEventListener('submit',e=>{e.preventDefault();sendChat();});
  $('chat-message').addEventListener('input',()=>{if(state.threadId)state.chatDrafts[state.threadId]=$('chat-message').value;sizeChatComposer();});
  $('chat-message').addEventListener('compositionstart',()=>{state.chatComposing=true;});
  $('chat-message').addEventListener('compositionend',()=>{state.chatComposing=false;});
  $('chat-message').addEventListener('keydown',e=>{
    if(e.key!=='Enter'||e.shiftKey||e.isComposing||state.chatComposing||e.keyCode===229)return;
    e.preventDefault();if(!e.repeat&&!$('chat-send').disabled)$('chat-form').requestSubmit();
  });
  async function syncFeedback(){
    const c=context();if(!c.projectId){errorAt('sync-feedback-error','请先从项目页进入要更新的项目。');return;}if(state.syncRun){trackRun(state.syncRun.id);return;}$('sync-feedback').disabled=true;setText('sync-feedback','正在读取…');setText('sync-feedback-status','正在检查 '+projectName(c.projectId)+' 的本地测试资料…');errorAt('sync-feedback-error','');
    try{const before=Object.fromEntries(feedbackSnapshot(c.projectId).map(f=>[f.id||f.feedback_id,feedbackVersion(f)]));state.syncOperation=state.syncOperation||uid();const id=await startRun('/sync',{operation_id:state.syncOperation},c);if(!sameSession(c))return;state.syncOperation=null;state.syncRun={id,before,projectId:c.projectId};trackRun(id);}
    catch(e){if(sameSession(c)){errorAt('sync-feedback-error',e);setText('sync-feedback-status','已有反馈保留。');$('sync-feedback').disabled=!state.projectId;setText('sync-feedback','检查一次');}}
  }
  $('sync-feedback').addEventListener('click',syncFeedback);$('manual-run').addEventListener('click',syncFeedback);
  $('toggle-runtime').addEventListener('click',()=>busy($('toggle-runtime'),async()=>{const c=context(),paused=!state.overview?.runtime?.paused;await api('/runtime',{method:'PATCH',body:{paused}});if(!sameSession(c))return;await refresh();setText('runtime-error',paused?'中央 Agent 与周期检查已暂停，外部 Local Agent 仍按各自安排运行。':'中央 Agent 已恢复运行。');},'runtime-error'));
  $('diagnostics-details').addEventListener('toggle',()=>{if(developerMode&&$('diagnostics-details').open&&!state.diagnostics)loadDiagnostics();});
  $('refresh-diagnostics').addEventListener('click',loadDiagnostics);
  $('new-task').addEventListener('click',()=>showTask());$('task-kind').addEventListener('change',taskKind);
  $('task-project').addEventListener('change',()=>{state.taskTargetId=$('task-project').value;taskMembers();errorAt('task-form-error','');});
  $('task-form').addEventListener('submit',async e=>{
    e.preventDefault();if(state.task?.display_redacted)return;const fields=taskFields(),targetProject=state.taskTargetId;if(!targetProject||!state.projects.some(p=>p.id===targetProject)){errorAt('task-form-error','请先选择任务所属项目。');return;}if(!fields.member_ids.length){errorAt('task-form-error','请至少选择一位适用成员。');return;}
    if(state.taskMode==='draft'&&['update','withdraw'].includes(state.task?.draft_action)&&!fields.reason){errorAt('task-form-error','请说明修改或停用的理由。');$('task-reason').focus();return;}
    const payloadKey=JSON.stringify(fields);if(!state.taskPending&&state.taskPayloadKey&&state.taskPayloadKey!==payloadKey)state.taskOperation=uid();state.taskPayloadKey=payloadKey;const item=state.task,mode=state.taskMode,op=state.taskOperation;
    await formAction('task-dialog','task-form-error',async c=>{
      setText('task-form-status',state.taskConflict?'正在核对最新任务并保存草稿…':state.taskPending||item?.publication_operation_id?'正在检查原发布结果…':'正在写入本地测试后端并核对…');
      try{
        if(mode==='draft'){
          if(state.taskConflict){
            const saved=await api('/task-drafts/'+encodeURIComponent(item.id)+'/edit',{method:'POST',body:{...fields,draft_action:item.draft_action}});
            if(!current(c))return;
            state.task=saved.item||saved;state.taskConflict=false;state.taskOperation=uid();state.taskPending=null;clearRecovery('task',item.id);
            setText('task-submit',state.task.draft_action==='withdraw'?'确认停用':state.task.draft_action==='update'?'发布更新':'发布任务');
            setText('task-form-status','草稿已按当前任务更新。请核对后再次确认。');errorAt('task-form-error','');
            return;
          }
          if(!item.publication_operation_id&&!state.taskPending&&(taskChanged(fields)||item.status==='rejected')){const saved=await api('/task-drafts/'+encodeURIComponent(item.id)+'/edit',{method:'POST',body:{...fields,draft_action:item.draft_action}});if(!current(c))return;state.task=saved.item||saved;}
          if(!state.task.draft_hash)throw new APIError('草稿版本无法确认，请刷新后重新审阅。');
          const body=state.taskPending?.body||{expected_hash:state.task.draft_hash,operation_id:op};state.taskPending={body,fields};saveRecovery('task',item.id,state.taskPending);
          await api('/task-drafts/'+encodeURIComponent(item.id)+'/publish',{method:'POST',body,timeout_ms:180000});
        }else{
          const body=state.taskPending?.body||{...fields,...(item?{version:String(Number(item.version||1)+1)}:{}),project_id:targetProject,operation_id:op};state.taskPending={body};saveRecovery('task',item?.task_id||'',state.taskPending);
          await api('/tasks',{method:'POST',body,timeout_ms:180000});
        }
        if(!current(c))return;clearRecovery('task',item?.id||item?.task_id||'');state.taskPending=null;close('task-dialog',true);await refresh();toast(mode==='draft'&&item?.draft_action==='withdraw'?'任务已停用，历史记录保留。':'本地测试任务已写入并核对。');
      }catch(error){if(current(c)){
          if(mode==='draft'){await refresh();const saved=list(workData()?.task_drafts).find(t=>t.id===item.id);if(saved)state.task=saved;}
          if(mode==='draft'&&error.code==='task_version_changed'){
            clearRecovery('task',item.id);state.taskPending=null;state.taskOperation=uid();state.taskConflict=true;state.task={...state.task,publication_operation_id:null};
            lockTask(item.draft_action==='withdraw',false);setText('task-submit','保存草稿并重新审阅');
            setText('task-form-status','先保存草稿并重新审阅，再确认发布。');
          }else if(uncertain(error)||state.task?.publication_operation_id){lockTask(true);show('task-reject',false);setText('task-submit','继续原发布');setText('task-form-status','请检查远端已有结果，再继续原发布。');}
          else{clearRecovery('task',item?.id||item?.task_id||'');state.taskPending=null;setText('task-form-status','发布未完成，输入已保留。');}
        }throw error;}
    });
  });
  function rejectTask(item){const c=context();confirm('退回任务草稿？','草稿不会发布。可说明需要调整的内容。','退回修改',async reason=>{await api('/task-drafts/'+encodeURIComponent(item.id)+'/reject',{method:'POST',body:{reason}});if(!current(c))return;close('task-dialog',true);await refresh();toast('草稿已退回。');},true);}
  $('task-reject').addEventListener('click',()=>rejectTask(state.task));
  $('change-my-password').addEventListener('click',()=>showPassword());
  $('password-dialog').addEventListener('close',()=>{$('password-form').reset();state.passwordTarget=null;});
  $('password-form').addEventListener('submit',e=>{
    e.preventDefault();if($('password-new').value!==$('password-confirm').value){errorAt('password-form-error','两次输入的新密码不一致。');return;}
    const target=state.passwordTarget,body={current_password:$('password-current').value,new_password:$('password-new').value,confirm_password:$('password-confirm').value};
    formAction('password-dialog','password-form-error',async c=>{
      try{await api(target?'/users/'+encodeURIComponent(target.id)+'/reset-password':'/auth/change-password',{method:'POST',body});}
      finally{body.current_password='';body.new_password='';body.confirm_password='';}
      if(!sameSession(c))return;close('password-dialog',true);
      if(target)toast('密码已重置，该成员需要重新登录。');
      else{state.loginRecovery=null;resetSession('密码已修改，请使用新密码重新登录。');$('login-password').focus();}
    });
  });
  $('add-user').addEventListener('click',()=>showUser());
  $('user-form').addEventListener('submit',async e=>{
    e.preventDefault();const roles=[$('user-admin').checked&&'admin',$('user-researcher').checked&&'researcher'].filter(Boolean);if(!roles.length){errorAt('user-form-error','请至少保留一种角色。');return;}
    const u=state.editUser,body={username:$('user-username').value.trim(),display_name:$('user-display').value.trim(),roles};if(!u&&$('user-password').value)body.password=$('user-password').value;
    await formAction('user-dialog','user-form-error',async c=>{setText('user-form-status','正在保存成员…');let data;try{data=await api(u?'/users/'+encodeURIComponent(u.id):'/users',{method:u?'PATCH':'POST',body});}finally{body.password='';$('user-password').value='';}if(!sameSession(c))return;close('user-dialog',true);if(u?.id===state.user.id&&data.relogin_required){state.loginRecovery=null;resetSession('账号信息已修改，请重新登录。');return;}await refresh();if(!sameSession(c))return;
      if(data.password){$('credential-username').value=data.user?.username||body.username;$('credential-password').value=data.password;setText('credential-close-warning','');open('credential-dialog');}else toast('成员信息已保存。');
    });
  });
  $('user-dialog').addEventListener('close',()=>{$('user-password').value='';});
  $('show-source-form').addEventListener('click',()=>show('source-form',$('source-form').classList.contains('hidden')));
  $('source-url').addEventListener('input',()=>{let folder=false;try{folder=new URL($('source-url').value).pathname.includes('/folder/');}catch{}$('source-watch').disabled=folder;if(folder)$('source-watch').checked=false;setText('source-watch-note',folder?'文件夹不支持正文变化检查。':'仅检查这份文档。');});
  $('source-form').addEventListener('submit',async e=>{
    e.preventDefault();const body={url:$('source-url').value.trim(),name:$('source-name').value.trim(),watch:$('source-watch').checked};
    const payloadKey=JSON.stringify(body);if(state.sourceOperation?.key!==payloadKey)state.sourceOperation={key:payloadKey,id:uid()};body.operation_id=state.sourceOperation.id;
    await formAction('source-form','source-form-error',async c=>{setText('source-form-status','正在读取并添加资料…');await api('/sources',{method:'POST',timeout_ms:180000,body});if(!sameSession(c))return;state.sourceOperation=null;$('source-form').reset();show('source-form',false);setText('source-form-status','资料已添加。');await refresh();});
  });
  $('copy-credential').addEventListener('click',()=>busy($('copy-credential'),async()=>{await navigator.clipboard.writeText('账号：'+$('credential-username').value+'\n初始密码：'+$('credential-password').value);errorAt('credential-close-warning','已复制。请妥善保存并私下交给该成员。');},'credential-close-warning'));
  $('credential-done').addEventListener('click',()=>close('credential-dialog',true));
  $('credential-dialog').addEventListener('close',()=>{$('credential-password').value='';$('credential-username').value='';});
  $('confirm-cancel').addEventListener('click',()=>{if(!state.forms.get('confirm-dialog')?.busy)close('confirm-dialog',true);});
  document.addEventListener('click',e=>{
    const b=e.target.closest('button');if(!b)return;
    if(b.dataset.close){close(b.dataset.close);return;}
    if(b.dataset.page&&(!['projects','agent','work','global','collaboration','knowledge','account'].includes(b.dataset.page)||(!developerMode&&b.dataset.page==='work')))return;
    if(b.dataset.page){if(!admin()&&['agent','work','global'].includes(b.dataset.page))return;if(b.dataset.page==='work'&&b.classList.contains('page-tab')&&state.projectId){clearProject();state.projectId=null;}state.page=b.dataset.page;if(state.page==='collaboration'&&!state.projectId){const project=state.projects.find(p=>p.is_default)||state.projects[0];if(project){selectProject(project.id,'collaboration');return;}}render();if(state.page==='collaboration')loadPhase3();if(state.page==='knowledge')loadKnowledge();if(['agent','global'].includes(state.page))loadRodos();maybeCheck();}
    if(b.dataset.projectViewSelect){navigation.projectId=null;renderProjects();}
    if(b.dataset.projectViewId&&state.projects.some(project=>project.id===b.dataset.projectViewId)){navigation.projectId=b.dataset.projectViewId;renderProjects();}
    if(['tasks','events','review'].includes(b.dataset.collaborationViewSelect)){navigation.collaboration=b.dataset.collaborationViewSelect;renderSidebar();}
    if(admin()&&['memory','accounts','sync'].includes(b.dataset.settingsViewSelect)){navigation.settings=b.dataset.settingsViewSelect;renderSidebar();loadRodos();}
    if(b.dataset.knowledgeCategory){
      if(knowledge.category===b.dataset.knowledgeCategory)knowledge.categoryExpanded=!knowledge.categoryExpanded;
      else{knowledge.category=b.dataset.knowledgeCategory;knowledge.categoryExpanded=true;knowledge.selectedId=null;knowledge.detail=null;knowledge.fileId=null;knowledge.fileDetail=null;knowledge.fileError='';knowledge.detailError='';knowledge.detailSequence++;knowledge.fileSequence++;}
      renderKnowledge();
    }
    if(b.dataset.knowledgeOpen)openKnowledge(b.dataset.knowledgeOpen);
    if(b.dataset.knowledgeSync)syncKnowledge(b.dataset.knowledgeSync,b,b.dataset.knowledgeSkill||null);
    if(b.dataset.knowledgeFile)openKnowledgeFile(b.dataset.knowledgeFile);
    if(b.dataset.knowledgeMain){knowledge.fileId=null;knowledge.fileDetail=null;knowledge.fileError='';knowledge.fileSequence++;renderKnowledge();}
    if(b.dataset.phase3Edit)showPhase3Task(phase3Tasks().find(t=>phase3TaskId(t)===b.dataset.phase3Edit));
    if(b.dataset.phase3EditDraft){const d=phase3Drafts().find(d=>d.id===b.dataset.phase3EditDraft);if(d)showPhase3Task(phase3Tasks().find(t=>phase3TaskId(t)===d.task_id)||null,d);}
    if(b.dataset.phase3Action)showPhase3Action(phase3Tasks().find(t=>phase3TaskId(t)===b.dataset.phase3Id),b.dataset.phase3Action);
    if(b.dataset.phase3Draft){const d=phase3Drafts().find(d=>d.id===b.dataset.phase3Draft);if(d)showPhase3Action(phase3Tasks().find(t=>phase3TaskId(t)===d.task_id)||{...d.payload,task_id:d.task_id},d.base_requirements_version?'update':'start',d);}
    if(b.dataset.phase3History)showPhase3History(b.dataset.phase3History,b);
    if(b.dataset.phase3Copy)busy(b,async()=>{const d=await api('/phase3/nodes/'+encodeURIComponent(b.dataset.phase3Copy)+'/onboarding',{method:'POST',body:{},timeout_ms:120000});await navigator.clipboard.writeText(d.instruction);toast(d.task?'已复制接入指令，安装与定时配置任务已就绪。':'已复制接入短指令。');},'member-management-error');
    if(b.dataset.tab){state.tab=b.dataset.tab;document.querySelectorAll('.tab').forEach(t=>{t.classList.toggle('active',t.dataset.tab===state.tab);t.setAttribute('aria-selected',String(t.dataset.tab===state.tab));});document.querySelectorAll('.tab-view').forEach(v=>v.classList.toggle('hidden',v.id!=='view-'+state.tab));}
    if(b.dataset.chatRetry){const pending=pendingChat();if(pending?.id===b.dataset.chatRetry)sendChat(pending);}
    if(b.dataset.chatExample){$('chat-message').value=b.dataset.chatExample;$('chat-message').focus();}
    if(b.dataset.projectPrompt)prepareProject(b.dataset.projectPrompt);
    if(b.dataset.projectWork)selectProject(b.dataset.projectWork,developerMode?'work':'collaboration');
    if(b.dataset.projectChat)selectProject(b.dataset.projectChat,'agent');
    if(b.dataset.editProject)showProject(state.projects.find(p=>p.id===b.dataset.editProject));
    if(b.dataset.deactivateProject)deactivateProject(b.dataset.deactivateProject,b);
    if(b.dataset.reactivateProject)reactivateProject(b.dataset.reactivateProject);
    if(b.dataset.editTask)showTask(list(workData()?.tasks).find(t=>String(t.task_id)===b.dataset.editTask));
    if(b.dataset.draftLifecycle)changeDraftLifecycle(b.dataset.draftKind,b.dataset.draftId,b.dataset.draftLifecycle,b);
    if(b.dataset.taskDraft)showTask(list(workData()?.task_drafts).find(t=>String(t.id)===b.dataset.taskDraft),'draft');
    if(b.dataset.withdrawTask){const t=list(workData()?.tasks).find(t=>String(t.task_id)===b.dataset.withdrawTask),op=uid(),c=context();if(!t?.project_id){errorAt('sync-feedback-error','无法确认这项任务所属项目，请刷新后重试。');return;}confirm('撤回这项任务？','任务将标记为已撤回，历史要求与反馈保留。','撤回任务',async()=>{await api('/tasks/'+encodeURIComponent(t.task_id)+'/withdraw',{method:'POST',body:{operation_id:op,project_id:t.project_id}});if(sameSession(c)){await refresh();toast('任务已撤回，历史记录保留。');}});}
    if(b.dataset.rejectTask)rejectTask(list(workData()?.task_drafts).find(t=>t.id===b.dataset.rejectTask));
    if(b.dataset.userEnabled)setMemberEnabled(allUsers().find(u=>String(u.id)===b.dataset.userEnabled));
    if(b.dataset.editUser)showUser(allUsers().find(u=>String(u.id)===b.dataset.editUser));
    if(b.dataset.resetPassword)showPassword(allUsers().find(u=>String(u.id)===b.dataset.resetPassword));
    if(b.dataset.proposal)showProposal(list(workData()?.proposals).find(p=>String(p.id)===b.dataset.proposal));
    if(b.dataset.removeSource){const id=b.dataset.removeSource,c=context();confirm('移除资料关联？','移除此资料入口，不删除飞书原文。','移除关联',async()=>{await api('/sources/'+encodeURIComponent(id),{method:'DELETE'});if(sameSession(c)){await refresh();toast('关联已移除，飞书原文保留。');}});}
    if(developerMode&&b.dataset.run)busy(b,async()=>{const c=context(),id=b.dataset.run;state.runViewSeq=(state.runViewSeq||0)+1;const seq=state.runViewSeq;const data=await api('/runs/'+encodeURIComponent(id));if(!current(c)||seq!==state.runViewSeq)return;state.selectedRun=data.run||data.item||data;renderRuns();open('workspace-drawer');$('workspace-drawer').scrollTop=0;},'chat-error');
    if(b.dataset.retryRun)busy(b,async()=>{const c=context(),id=b.dataset.retryRun;const data=await api('/runs/'+encodeURIComponent(id)+'/retry',{method:'POST',body:{}});if(!current(c))return;const next=data.run_id||data.id||data.item?.id||id;const previous=state.activeRun?.id===id?state.activeRun:state.selectedRun?.id===id?state.selectedRun:state.runItems.find(r=>r.id===id);state.activeRun={...previous,id:next,status:'queued'};renderActiveRun();renderConnection();trackRun(next);await refresh();},'chat-error');
    if(b.id==='edit-proposal'){state.proposalEditing=!state.proposalEditing;show('proposal-editor',state.proposalEditing);show('proposal-preview',!state.proposalEditing);b.textContent=state.proposalEditing?'查看草稿预览':'编辑草稿';if(!state.proposalEditing&&proposalChanged())put('proposal-preview','<section><h3>修改前</h3><div class="markdown-content">'+markdown($('proposal-old').value)+'</div></section><section><h3>修改后（未保存）</h3><div class="markdown-content">'+markdown($('proposal-new').value)+'</div></section>');updatePublishButton();}
    if(b.id==='save-proposal'){const id=state.proposal.id,body={old_text:$('proposal-old').value,new_text:$('proposal-new').value,summary:$('proposal-summary').value};formAction('proposal-dialog','proposal-form-error',async c=>{const data=await api('/proposals/'+encodeURIComponent(id)+'/edit',{method:'POST',body});if(!current(c))return;showProposal(data.item||data);await refresh();toast('草稿已保存，请审阅后确认发布。');});}
    if(b.id==='reject-proposal'){const id=state.proposal.id,c=context();confirm('退回方法建议？','本次修改不会发布，可说明需要调整的内容。','退回修改',async reason=>{await api('/proposals/'+encodeURIComponent(id)+'/reject',{method:'POST',body:{reason}});if(current(c)){close('proposal-dialog',true);await refresh();toast('方法建议已退回。');}},true);}
    if(b.id==='publish-proposal'){if(state.proposalSourceChanged)return;const item=state.proposal,op=state.publishOperation;formAction('proposal-dialog','proposal-form-error',async c=>{if(proposalChanged()||!item?.draft_hash)throw new APIError('请先保存草稿修改，再确认发布。');try{await api('/proposals/'+encodeURIComponent(item.id)+'/publish',{method:'POST',body:{expected_hash:item.draft_hash,operation_id:op},timeout_ms:180000});if(!current(c))return;close('proposal-dialog',true);await refresh();toast(item.target_kind==='template'?'新生成的指令已生效；成员读取和使用仍待反馈。':'修改已写入本地测试后端，真实采用效果未验证。');}catch(e){if(current(c)){if(['source_changed','patch_conflict'].includes(e.code)){state.proposalSourceChanged={id:item.id,base_hash:item.base_hash};throw new APIError(proposalStaleMessage(item),e.status,e.code,e.technical);}await refresh();const saved=list(workData()?.proposals).find(p=>p.id===item.id);if(uncertain(e)||saved?.publication_operation_id){state.proposal=saved?.publication_operation_id?saved:{...item,publication_operation_id:op};showProposal(state.proposal);}}throw e;}});}
  });
  $('rodos-agent-tab')?.addEventListener('click',()=>{rodos.tab='agent';renderRodos();});
  $('rodos-memory-tab')?.addEventListener('click',()=>{rodos.tab='memory';renderRodos();});
  $('rodos-settings-refresh')?.addEventListener('click',()=>loadRodos(true));
  $('rodos-memory-inactive')?.addEventListener('change',renderRodos);
  $('rodos-policy-content')?.addEventListener('input',()=>{rodos.policyDirty=true;rodos.policyOp=null;});
  $('rodos-memory-form')?.addEventListener('input',()=>{rodos.memoryDirty=true;rodos.memoryOp=null;});
  $('rodos-memory-scope')?.addEventListener('change',()=>show('rodos-memory-project-label',$('rodos-memory-scope').value==='project'));
  $('rodos-memory-add')?.addEventListener('click',()=>editMemory());
  $('rodos-memory-cancel')?.addEventListener('click',()=>{if(rodos.memoryDirty&&!window.confirm('放弃尚未保存的记忆修改？'))return;rodos.edit=null;rodos.memoryDirty=false;show('rodos-memory-form',false);});
  $('rodos-policy-form')?.addEventListener('submit',e=>{e.preventDefault();busy($('rodos-policy-save'),async()=>{
    if(!rodos.policy)throw new Error('请先读取当前职责。');
    if(rodos.policyConflict)throw new Error('请先核对下方的新版本，编辑内容已保留。');
    const c=context(),content=$('rodos-policy-content').value,body={content,expected_version:rodos.policy.version,operation_id:rodos.policyOp||(rodos.policyOp=uid())};
    try{const result=await api('/settings/agent',{method:'PUT',body});if(!sameSession(c))return;rodos.policy=result;rodos.policyDirty=$('rodos-policy-content').value!==content;rodos.policyOp=null;setText('rodos-policy-status',result.file_updated?'职责已保存，下一次运行生效。':'职责已保存，文件待更新。');errorAt('rodos-policy-error',result.file_error||'');renderRodos();}
    catch(error){if(error.status===409&&sameSession(c)){const detail=JSON.parse(error.technical||'{}');rodos.policyConflict=detail.current;put('rodos-policy-conflict','<details open><summary>当前已保存的新版本</summary><pre>'+esc(detail.current?.content||'请重新读取')+'</pre><button type="button" class="secondary" data-policy-rebase="true">已核对，保留我的输入继续编辑</button></details>');}throw error;}
  },'rodos-policy-error');});
  $('rodos-memory-form')?.addEventListener('submit',e=>{e.preventDefault();busy($('rodos-memory-save'),async()=>{
    const item=rodos.edit||{},c=context(),scope=$('rodos-memory-scope').value,body={operation:item.id?'correct':'remember',operation_id:rodos.memoryOp||(rodos.memoryOp=uid()),expected_version:item.version||0,memory_id:item.id||'',topic:$('rodos-memory-topic').value,content:$('rodos-memory-content').value,scope,project_id:scope==='project'?$('rodos-memory-project').value:''};
    const snapshot={topic:body.topic,content:body.content};const result=await api('/settings/memories',{method:'POST',body});if(!sameSession(c))return;
    if($('rodos-memory-topic').value!==snapshot.topic||$('rodos-memory-content').value!==snapshot.content){rodos.memoryOp=null;setText('rodos-memory-status','提交时的版本已保存；您之后的输入仍保留，请重新核对版本后继续。');await loadRodos(true);return;}
    rodos.memoryDirty=false;rodos.edit=null;rodos.memoryOp=null;show('rodos-memory-form',false);setText('rodos-memory-status',result.summary_updated===false?'记忆已保存，摘要文件待更新。':'记忆已保存。');await loadRodos(true);
  },'rodos-memory-error');});
  $('rodos-refresh-feishu')?.addEventListener('click',()=>startMaintenance('refresh',$('rodos-refresh-feishu')));
  $('rodos-inspect-now')?.addEventListener('click',()=>startMaintenance('inspect',$('rodos-inspect-now')));
  document.addEventListener('click',event=>{const b=event.target.closest('button');if(!b||!admin())return;
    if(b.dataset.memoryEdit)editMemory(rodos.memories.find(m=>m.id===b.dataset.memoryEdit));
    for(const [attr,kind,title] of [['memoryInvalidate','invalidate','让这项约定失效？'],['memoryForget','forget','忘记这项约定？']])if(b.dataset[attr]){const item=rodos.memories.find(m=>m.id===b.dataset[attr]);if(item)confirm(title,kind==='forget'?'停止召回并抑制原来源重新提取；历史审计与备份仍保留。':'保留历史，不再按有效约定使用。',kind==='forget'?'忘记':'设为失效',()=>changeMemory(kind,item));}
    if(b.dataset.policyRebase&&rodos.policyConflict){rodos.policy=rodos.policyConflict;rodos.policyConflict=null;rodos.policyOp=null;put('rodos-policy-conflict','');renderRodos();errorAt('rodos-policy-error','已以当前版本为基础，您的输入保留，请核对后保存。');}
    if(b.dataset.rodosSchedule)busy(b,async()=>{const s=rodos.maintenance?.schedules.find(s=>s.id===b.dataset.rodosSchedule);if(!s)return;await api('/maintenance/schedule',{method:'PATCH',body:{schedule_id:s.id,enabled:!s.enabled,expected_revision:s.revision}});await loadRodos();},'rodos-maintenance-error');
    if(b.dataset.rodosRun)busy(b,async()=>{const c=context(),run=await api('/maintenance/runs/'+encodeURIComponent(b.dataset.rodosRun));if(sameSession(c))put('rodos-latest-inspection',maintenanceCard(run,true));},'rodos-agent-maintenance-error');
    if(b.dataset.peerApprove)approvePeer(b.dataset.peerApprove,b.dataset.peerRequest,b);
  });

  $('proposal-dialog').addEventListener('input',updatePublishButton);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.user){refresh();maybeCheck();}});window.addEventListener('focus',maybeCheck);
  document.querySelectorAll('dialog').forEach(dialog=>{
    dialog.addEventListener('cancel',e=>{e.preventDefault();close(dialog.id);});
    dialog.addEventListener('click',e=>{if(e.target!==dialog)return;const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)close(dialog.id);});
  });
  window.addEventListener('beforeunload',e=>{if(rodos.policyDirty||rodos.memoryDirty||[...state.forms.entries()].some(([id,f])=>$(id).open&&(f.busy||formValue(id)!==f.initial))||$('chat-message').value||$('credential-dialog').open){e.preventDefault();e.returnValue='';}});
  (async()=>{try{
    const formal=location.pathname.startsWith('/baite-rdos/');
    const response=await fetch(formal?'api/runtime/info':'api/lab/info',{credentials:'same-origin',cache:'no-store'});
    if(!response.ok)throw new Error('服务状态读取失败，请刷新后重试。');
    labInfo=await response.json();
    if(!labInfo.env_id||!labInfo.generation||labInfo.project_id!==(formal?'project_rdos':'lab_project_phase0'))throw new Error('服务配置不匹配，请联系管理员。');
    if(developerMode)setText('lab-environment',labInfo.env_id+' · 代次 '+labInfo.generation);
    if(developerMode)setText('lab-capability','文档 '+(labInfo.runtime?.phase3_backend||'local_mock')+' · 中央模型 '+(labInfo.runtime?.model_mode||'mock')+' · 中央长期调度关闭。');
    $('login-submit').disabled=false;
    const data=await api('/auth/me');beginSession(data.user||data);await refresh();
  }catch(e){if(e.status!==401)setText('login-error',e.message);}})();
})();
