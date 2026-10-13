(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const list = (v) => Array.isArray(v) ? v : [];
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const labels = { admin:"管理员", researcher:"研究员", actual:"实际身份", role_test:"角色测试", active:"有效", published:"已发布", draft:"待审阅", rejected:"已退回", pending:"待核对", blocked:"需要补充", withdrawn:"已撤回", running:"运行中", queued:"等待运行", succeeded:"已完成", completed:"已完成", failed:"失败", interrupted:"运行中断", cancelled:"已取消", paused:"已暂停", verified:"可核验结果", self_reported:"自述", unverified:"尚未验证", current:"当前版本", stale:"旧版本反馈", unknown:"待核对" };
  const label = (v) => labels[v] || String(v || "待核对");
  const staleProposalMessage = '来源已变化，当前草稿保留。关闭后请在中央 Agent 中重新读取来源并起草。';
  const proposalStaleMessage = item => item?.target_kind==='template'?'指令模板已有新版本，草稿保留。请根据当前模板重新起草后审阅。':staleProposalMessage;
  const pill = (v, tone = "") => '<span class="pill ' + tone + '">' + esc(label(v)) + '</span>';
  const state = { user:null, workspace:null, workspaces:{}, overview:null, page:"onboard", tab:"tasks", projectId:null, projects:[], sources:[], onboarding:null, onboardingError:"", onboardingCheckError:"", onboardingRequestSeq:0, instructionError:"", reentering:false, onboardingStarting:false, onboardingStartSeq:0, instruction:null, instructionPromise:null, projectInstruction:null, checking:false, copied:false, lastCheck:0, activeRun:null, selectedRun:null, task:null, taskMode:"manual", taskConflict:false, editUser:null, editProject:null, proposal:null, proposalEditing:false, proposalSourceChanged:null, diagnostics:null, diagnosticsLoading:false, diagnosticsError:"", diagnosticsSeq:0, polling:new Map(), refreshTimer:null, refreshPromise:null, toastTimer:null, generation:0, session:0, refreshSeq:0, instructionSeq:0, projectInstructionSeq:0, errors:{}, loaded:{}, chatOperation:null, chatSubmitting:false, syncRun:null, chatDrafts:{}, forms:new Map(), taskOperation:null, projectOperation:null, sourceOperation:null, publishOperation:null };
  const context = () => ({session:state.session, generation:state.generation, projectId:state.projectId});
  const current = c => !!state.user && c.session===state.session && c.generation===state.generation && c.projectId===state.projectId;
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
    try { const u = new URL(v, location.href); return ["http:","https:"].includes(u.protocol) ? u.href : null; } catch { return null; }
  };
  const link = (url, title = "打开原文 ↗") => safeUrl(url) ? '<a href="'+esc(safeUrl(url))+'" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">'+esc(title)+'</a>' : "";
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
  const allUsers = () => list(state.overview?.users).length ? list(state.overview.users) : list(Object.values(state.workspaces).find(w=>list(w?.users).length)?.users);
  const member = (id) => allUsers().find(u => String(u.id) === String(id));
  const memberName = (id) => (member(id)?displayName(member(id)):id) || "未标记成员";
  const selectedWork = () => state.projectId ? state.workspaces[state.projectId] || state.workspace : null;
  function workData(){
    if(state.projectId)return selectedWork();
    if(state.overview && Array.isArray(state.overview.tasks))return state.overview;
    const workspaces=Object.values(state.workspaces);
    if(!workspaces.length)return null;
    const unique=name=>[...new Map(workspaces.flatMap(w=>list(w?.[name])).map(item=>[String(item.id||item.task_id||item.feedback_id),item])).values()];
    return {tasks:unique('tasks'),task_drafts:unique('task_drafts'),feedback:unique('feedback'),proposals:unique('proposals')};
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
        else if(response.status===422)message='请检查表单中必填内容和格式，再重新提交。';
        else if(!message||!/[\u3400-\u9fff]/.test(message))message=response.status>=500?'服务暂时无法完成操作，请稍后重试。':'操作未完成，请检查当前内容后重试。';
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
  const formValue=id=>JSON.stringify([...$(id).querySelectorAll('input,textarea,select')].map(n=>[n.id,n.type==='checkbox'?n.checked:n.value]));
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
    finally{f.busy=false;if(sameSession(f.context)){inputs.forEach((n,i)=>{if(n.isConnected)n.disabled=wasDisabled[i];});if(id==='project-dialog'&&state.projectPending&&!state.editProject?.id)$(id).querySelectorAll('input,textarea').forEach(n=>n.type==='checkbox'?n.disabled=true:n.readOnly=true);if(id==='task-dialog'&&state.taskPending)lockTask(true);buttons.forEach((b,i)=>{if(b.isConnected){b.disabled=disabled[i];b.removeAttribute('aria-busy');}});if(id==='proposal-dialog'&&$(id).open)updatePublishButton();}}
  }
  function copyKey(){return ['rdos-copied',state.user?.id,state.onboarding?.session_id,state.onboarding?.stage,state.instruction?.id||state.instruction?.url].join(':');}
  function copiedValue(){try{return !!state.instruction&&sessionStorage.getItem(copyKey())==='yes';}catch{return false;}}
  function clearProject(resetOnboarding=false){
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
    state.loginRecovery={source:!$('source-form').classList.contains('hidden')?{url:$('source-url').value,name:$('source-name').value,watch:$('source-watch').checked,operation:state.sourceOperation}:null,userId:state.user.id,projectId:state.projectId,chat:$('chat-message').value,dialog,fields,
      task:state.task,taskMode:state.taskMode,proposal:state.proposal,editProject:state.editProject,editUser:state.editUser,
      taskOperation:state.taskOperation,projectOperation:state.projectOperation,publishOperation:state.publishOperation};
  }
  function restoreLoginDraft(){
    const d=state.loginRecovery;if(!d||d.userId!==state.user?.id)return;
    if(d.projectId!==state.projectId){state.loginRecovery=null;return;}
    state.loginRecovery=null;$('chat-message').value=d.chat||'';
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
    document.querySelectorAll('[aria-busy]').forEach(b=>{b.removeAttribute('aria-busy');if(b.tagName==='BUTTON')b.disabled=false;});state.session++;clearProject(true);state.user=null;state.projectId=null;state.projects=[];state.sources=[];state.overview=null;state.workspaces={};state.chatDrafts={};state.page='onboard';state.forms.clear();state.task=null;state.proposal=null;state.editProject=null;state.editUser=null;
    state.sourceOperation=null;state.proposalSourceChanged=null;state.diagnostics=null;state.diagnosticsLoading=false;state.diagnosticsError='';state.diagnosticsSeq++;$('diagnostics-details').open=false;put('diagnostics-result','');setText('diagnostics-status','');errorAt('diagnostics-error','');$('source-form').reset();show('source-form',false);errorAt('source-form-error','');setText('source-form-status','');
    clearInterval(state.refreshTimer);document.querySelectorAll('dialog[open]').forEach(d=>d.close());
    show('app-view',false);show('login-view');$('messages').replaceChildren();delete $('messages').dataset.rendered;$('chat-message').value='';$('credential-password').value='';$('user-password').value='';setText('login-error',message);
  }
  function beginSession(user){resetSession();if(state.loginRecovery&&state.loginRecovery.userId!==user.id)state.loginRecovery=null;state.user=user;state.projectId=state.loginRecovery?.projectId||null;show('login-view',false);show('app-view');render();state.refreshTimer=setInterval(()=>{if(!document.hidden&&state.user){refresh();maybeCheck();}},30000);}
  async function refresh(){
    if(!state.user)return {ok:false};if(state.refreshPromise)return state.refreshPromise;
    const c=context(),seq=++state.refreshSeq,onboardingSeq=state.checking||state.onboardingStarting?null:++state.onboardingRequestSeq;
    const work=async()=>{
      let projects;
      try{projects=list((await api('/projects')).items);}catch(e){if(!current(c))return {ok:false};state.errors.projects=e.message;}
      if(!current(c)||seq!==state.refreshSeq)return {ok:false};
      if(projects){state.projects=projects;state.loaded.projects=true;delete state.errors.projects;
        if(state.projectId&&!projects.some(p=>p.id===state.projectId)){
          clearProject();state.projectId=null;state.selectionNotice='原项目已不可访问，现显示全部项目。';
          c.generation=state.generation;c.projectId=null;
        }
      }
      renderNavigation();
      const onboardingStartSeq=state.onboardingStartSeq;
      const requests=[state.projectId?api('/workspace?project_id='+encodeURIComponent(state.projectId)):Promise.resolve(null),onboardingSeq!==null&&onboardingSeq===state.onboardingRequestSeq&&!state.checking&&!state.onboardingStarting?api('/onboarding/status'):Promise.resolve(null),admin()?api('/sources'):Promise.resolve({items:[]}),admin()?api('/overview'):Promise.resolve(null)];
      const results=await Promise.allSettled(requests);
      if(!current(c)||seq!==state.refreshSeq)return {ok:false};
      if(results[0].status==='fulfilled'&&results[0].value?.user?.id&&results[0].value.user.id!==state.user.id){resetSession('当前登录账号已变化，请重新登录。');return {ok:false};}
      ['workspace','onboarding','sources','overview'].forEach((name,i)=>{
        if(name==='onboarding'&&(onboardingSeq===null||onboardingSeq!==state.onboardingRequestSeq||state.checking||state.onboardingStarting||state.onboardingStartSeq!==onboardingStartSeq))return;
        const r=results[i];if(r.status==='rejected'){state.errors[name]=r.reason.message;if(name==='onboarding')state.onboardingError=r.reason.message;return;}
        delete state.errors[name];state.loaded[name]=true;
        if(name==='workspace'){state.workspace=r.value;if(r.value&&state.projectId)state.workspaces[state.projectId]=r.value;if(r.value?.user)state.user=r.value.user;}
        if(name==='onboarding'){state.onboardingError='';if(r.value)acceptOnboarding(r.value);}
        if(name==='sources')state.sources=list(r.value.items);
        if(name==='overview'&&r.value)state.overview=r.value;
      });
      render();
      if(admin())for(const run of [...list(state.overview?.runs),...list(state.workspace?.runs)])if(running(run)){if(run.kind!=='sync'&&run.trigger!=='scheduled'&&!running(state.activeRun))state.activeRun=run;trackRun(run.id);}
      renderConnection();restoreLoginDraft();const names={projects:'项目列表',workspace:'项目内容',onboarding:'接入状态',sources:'共享资料',overview:'团队概览'};
      const failures=Object.keys(state.errors).map(k=>names[k]||k);return {ok:!failures.length,failures};
    };
    const pending=work();state.refreshPromise=pending;
    try{return await pending;}catch(e){if(current(c)){showError(e.message);return {ok:false,failures:['当前页面']};}}finally{if(state.refreshPromise===pending)state.refreshPromise=null;}
  }
  const onboardingKey=data=>JSON.stringify([data?.session_id,data?.choice,data?.stage,data?.stage_status,list(data?.completed_steps),list(data?.missing),data?.ready_for_work]);
  const continuationKey=data=>['rdos-onboarding-continued',state.user?.id,data?.session_id,data?.stage].join(':');
  const continued=data=>{try{return sessionStorage.getItem(continuationKey(data))==='yes';}catch{return false;}};
  const nextOnboardingStep=data=>data&&!data.ready_for_work&&data.stage_status==='pending'&&list(data.completed_steps).length&&[1,3].includes(Number(data.stage))&&!continued(data)?Number(data.stage):null;
  function acceptOnboarding(data){
    if(state.onboarding&&onboardingKey(state.onboarding)!==onboardingKey(data)){state.instruction=null;state.instructionSeq++;state.instructionPromise=null;state.copied=false;}
    state.onboarding=data;
    if(data.choice&&!data.ready_for_work&&!nextOnboardingStep(data)&&!data.error&&!(data.stage_status==='blocked'&&!list(data.missing).length)&&!state.instruction&&!state.instructionPromise){const c=onboardingContext();prepareInstruction(false).catch(e=>{if(currentOnboarding(c)){state.instructionError=e.message;renderOnboarding();}});}
  }
  const pageInfo={onboard:['Agent 接入',''],projects:['项目',''],agent:['中央 Agent',''],work:['任务与反馈',''],global:['全局设置','']};
  function renderNavigation(){
    if(!admin()&&['agent','work','global'].includes(state.page))state.page='onboard';
    document.querySelectorAll('.admin-only:not(.main-page)').forEach(el=>el.classList.toggle('hidden',!admin()));
    document.querySelectorAll('.page-tab').forEach(el=>{const yes=el.dataset.page===state.page;el.classList.toggle('active',yes);el.setAttribute('aria-current',yes?'page':'false');});
    document.querySelectorAll('.main-page').forEach(el=>el.classList.toggle('hidden',el.id!=='page-'+state.page||(el.classList.contains('admin-only')&&!admin())));
    setText('page-title',state.page==='projects'&&!admin()?'我的项目':pageInfo[state.page][0]);setText('page-description',pageInfo[state.page][1]);
    show('refresh-workspace',state.page!=='onboard');
    show('new-project',admin());
  }
  function render(){
    if(!state.user)return;
    put('current-user','<span>'+esc(displayName(state.user))+'</span>'+list(state.user.roles).map(r=>pill(r)).join('')+(state.user.identity_mode==='role_test'?pill('角色测试','warn'):''));
    renderNavigation();
    const names={projects:'项目列表',workspace:'项目内容',onboarding:'接入状态',sources:'共享资料',overview:'团队概览'};
    const failures=Object.entries(state.errors).filter(([k])=>state.page!=='onboard'&&k!=='onboarding'&&!(k==='workspace'&&state.page==='agent')).map(([k,v])=>(names[k]||k)+'：'+v);
    showError(failures.length?'更新失败，'+(state.onboarding||state.workspace||state.loaded.projects?'当前显示上次内容。':'请重试。')+' '+failures.join(' '):state.page==='onboard'?'':state.selectionNotice||'');
    renderOnboarding();renderProjects();
    if(admin()){renderConnection();renderMessages();renderWorkScope();renderTasks();renderTaskDrafts();renderFeedback();renderProposals();renderPeople();renderRuntime();renderDiagnostics();renderSources();renderRuns();}
  }
  function renderConnection(){
    if(!admin())return;const r=state.overview?.runtime||state.workspace?.runtime||{};
    const notices=[];
    if(!state.overview)notices.push(state.errors.overview?'团队信息读取失败，请刷新重试。':'正在读取团队信息…');
    else{if(!r.model_configured)notices.push('当前模型尚未配置，请联系管理员检查本机模型连接。');if(!r.feishu_configured)notices.push('飞书连接尚不可用，请在全局设置检查连接。');if(r.paused)notices.push('中央 Agent 已暂停。可在全局设置恢复运行。');}
    const latest=[...list(state.overview?.runs)].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')))[0];
    const failed=latest&&['failed','interrupted','cancelled'].includes(latest.status);
    if(r.error&&(!latest||failed))notices.push(latest?'上次运行未完成。':'此前运行未完成。');
    put('connection-banner',notices.map(esc).join(' ')+(r.error&&(!latest||failed)?'<button class="quiet" type="button" '+(latest?.id?'data-run="'+esc(latest.id)+'"':'data-page="global"')+'>'+(latest?.id?'查看这次运行':'查看运行状态')+'</button>':''));show('connection-banner',!!notices.length);
    const working=running(state.activeRun);setText('agent-status',state.chatSubmitting?'正在提交':working?'正在处理':r.paused?'已暂停':r.model_configured?'可以对话':'暂不可用');
    $('chat-send').disabled=!state.overview||!r.model_configured||working||state.chatSubmitting||!!r.paused;
    setText('chat-send',state.chatSubmitting?'正在提交…':working?'正在处理…':'发送');$('chat-send').toggleAttribute('aria-busy',working||state.chatSubmitting);$('chat-message').disabled=false;
  }
  function renderMessages(){
    if(state.page!=='agent')return;
    const firstView=state.chatViewed!=='organization';
    const box=$('messages'),atEnd=box.scrollHeight-box.scrollTop-box.clientHeight<70;
    const runs=new Map([...list(state.overview?.runs),...Object.values(state.workspaces).flatMap(w=>list(w?.runs))].map(r=>[r.id,r]));
    const all=[...list(state.overview?.messages),...Object.values(state.workspaces).flatMap(w=>list(w?.messages))];
    const messages=[...new Map(all.map(m=>[m.id||m.run_id+':'+m.role,m])).values()].filter(m=>{const r=runs.get(m.run_id);return m.trigger!=='scheduled'&&m.kind!=='sync'&&r?.trigger!=='scheduled'&&r?.kind!=='sync';}).sort((a,b)=>String(a.created_at||'').localeCompare(String(b.created_at||'')));
    put('messages',!state.overview?empty(state.errors.overview?'对话暂时无法读取':'正在读取对话',state.errors.overview?'请点击刷新，已有草稿保留。':''):messages.length?messages.map(m=>'<article class="message '+(m.role==='user'?'user':'agent')+'" data-key="'+esc(m.id||m.run_id+':'+m.role)+'"><span class="message-name">'+(m.role==='user'?'你':'中央 Agent')+(m.project_id?' · '+esc(projectName(m.project_id)):'')+'</span><div class="message-body markdown-content">'+markdown(textValue(m.content))+'</div>'+(m.run_id?'<button class="quiet" type="button" data-run="'+esc(m.run_id)+'">查看执行详情</button>':'')+'</article>').join(''):empty('开始和中央 Agent 讨论')+'<div class="chat-examples">'+['最近有哪些工作需要介入？','帮我准备一项任务','哪些方法值得更新？'].map(t=>'<button class="secondary" type="button" data-chat-example="'+esc(t)+'">'+t+'</button>').join('')+'</div>');
    if(firstView&&messages.length){
      let latestQuestion=messages.length-1;
      for(let i=messages.length-1;i>=0;i--)if(messages[i].role==='user'){latestQuestion=i;break;}
      const target=box.querySelectorAll('.message')[latestQuestion];
      if(target)box.scrollTop=Math.max(0,box.scrollTop+target.getBoundingClientRect().top-box.getBoundingClientRect().top-8);
      state.chatViewed='organization';
    }else if(atEnd&&state.chatViewed==='organization')box.scrollTop=box.scrollHeight;
    renderActiveRun();
  }
  function renderActiveRun(){show('active-run',!!state.activeRun&&!['succeeded','completed'].includes(state.activeRun.status));if(state.activeRun)put('active-run',running(state.activeRun)?'<p class="checking-status"><span class="activity-dot"></span>正在处理，可以继续写下一条消息。</p>':runContent(state.activeRun,true));}
  function stepSources(step) {
    let r=step.result; if (typeof r==="string") { try { r=JSON.parse(r); } catch { r=null; } }
    const known=[...list(state.overview?.sources),...list(state.workspace?.sources)].find(s => s.id===(step.arguments?.object_id||step.arguments?.id));
    return [...new Set([step.source_url,r?.source_url,known?.source_url,...list(r?.items).map(x=>x.source_url),...list(r?.sources).map(x=>x.source_url||x.url)].map(safeUrl).filter(Boolean))].slice(0,8).map(u=>link(u,"打开实际来源 ↗")).join(" · ");
  }
  function unavailable(){return empty(workError()?'内容暂时无法读取':'正在加载',workError()?'请点击刷新重试，已有内容保留。':'');}
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
    put('task-list',!data?unavailable():tasks.length?tasks.map(t=>'<article class="item-card '+(withdrawn(t)?'withdrawn':'')+'" data-key="task:'+esc(t.task_id)+'"><div class="row"><h3>'+esc(taskTitle(t))+'</h3>'+(withdrawn(t)?pill('已撤回'):['已发布','published','active'].includes(t.status)?'':pill(t.status))+'</div><div class="item-meta">'+(!state.projectId?'<span>'+esc(projectName(t.project_id))+'</span>':'')+'<span>适用：'+esc(list(t.member_ids).map(memberName).join('、')||'组织共享接入要求')+'</span>'+(t.updated_at||t.created_at?'<span>'+date(t.updated_at||t.created_at)+'</span>':'')+'</div><details><summary>查看任务要求</summary><div class="markdown-content">'+markdown(t.instructions)+'</div><p>版本 '+esc(t.version||1)+(t.task_kind==='onboarding'?' · '+esc(t.stage):'')+' · '+esc(t.nature||'一次性')+'</p><details><summary>任务标识与关联</summary><p class="mono">'+esc(t.task_id)+(t.related_task?' · '+esc(t.related_task):'')+'</p></details></details><div class="item-actions">'+link(t.source_url,'查看飞书任务')+(!withdrawn(t)&&t.project_id?'<button class="quiet" data-edit-task="'+esc(t.task_id)+'" type="button">编辑任务</button><button class="quiet danger" data-withdraw-task="'+esc(t.task_id)+'" type="button">撤回任务</button>':'')+'</div></article>').join(''):empty('暂无已发布任务','可以新建任务，或请中央 Agent 起草。'));
  }
  function renderTaskDrafts(){
    const data=workData(),items=list(data?.task_drafts);setText('draft-count',items.filter(x=>x.status==='draft').length+' 待审阅');
    put('task-draft-list',!data?unavailable():items.length?items.map(t=>'<article class="item-card" data-key="draft:'+esc(t.id)+'"><div class="row"><h3>'+esc(taskTitle(t))+'</h3>'+pill(t.publication_operation_id&&t.status!=='published'?'发布待核对':t.status==='draft'?'待审阅':t.status,t.status==='draft'?'warn':'')+'</div><p>'+esc(draftActionLabel(t))+' · '+(!state.projectId?esc(projectName(t.project_id))+' · ':'')+'适用：'+esc(list(t.member_ids).map(memberName).join('、')||'待确认')+'</p>'+(t.reason?'<p>理由：'+esc(t.reason)+'</p>':'')+(t.rejection_reason?'<p>退回说明：'+esc(t.rejection_reason)+'</p>':'')+'<details><summary>查看原任务与版本</summary><p>'+esc(t.task_id||'新任务')+(t.base_task_version?' · 原版本 '+esc(t.base_task_version):'')+'</p></details><div class="item-actions">'+(t.status!=='published'?'<button class="secondary" data-task-draft="'+esc(t.id)+'" type="button">'+(t.publication_operation_id?'检查发布结果':'审阅任务草稿')+'</button>'+(!t.publication_operation_id?'<button class="quiet danger" data-reject-task="'+esc(t.id)+'" type="button">退回修改</button>':''):link(t.source_url,'查看已发布任务'))+'</div></article>').join(''):empty('暂无任务草稿','可请中央 Agent 起草任务。'));
  }
  function draftActionLabel(t){return ({create:'新建任务',update:'修改任务',withdraw:'停用任务'})[t?.draft_action]||'任务草稿';}
  const feedbackSectionLabels={progress:'当前进度',outputs:'本期成果',blockers:'需要协作',human_gate:'评审与确认线索',skill_rag:'Skill／RAG 使用',local_skill:'本地 Skill 更新'};
  const feedbackVersionStatus=v=>['stale','旧版本反馈'].includes(v)?'旧版本反馈':['current','当前版本'].includes(v)?'当前版本':['mixed','部分旧版本'].includes(v)?'部分任务版本需核对':'版本待核对';
  function renderFeedback(){
    const data=workData(),items=list(data?.feedback);setText('feedback-count',items.length+' 条');
    put('feedback-list',!data?unavailable():items.length?items.map(f=>{
      const taskRefs=list(f.task_refs),associated=!!f.task_id||taskRefs.length>0;
      const readback=f.source_readback===true||(f.source_readback==null&&!!f.source_url&&!!f.content_hash&&String(f.evidence_status||'').includes('正文已从飞书读回'));
      const evidence=readback?'已收到反馈':f.evidence_status==='self_reported'?'Agent 报告':'尚未读回';
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
      const detail='<details><summary>查看完整反馈与依据</summary><div class="markdown-content">'+markdown(textValue(f.content))+'</div>'+(f.observed_at?'<p class="small-note">系统读取：'+date(f.observed_at)+'</p>':'')+'<p class="small-note">'+(readback?'原文已读回。工作效果仍以具体成果和后续使用为准。':'原文尚未读回，请先更新反馈。')+(version==='旧版本反馈'?'旧版本反馈仍可参考。':'')+'</p>'+(associated?'<p>关联任务：'+refs+'</p>':'')+(f.correction_of?'<p>更正原反馈：'+esc(f.correction_of)+'</p>':'')+(f.template_version?'<p>反馈说明版本：'+esc(f.template_version)+'</p>':'')+'<details><summary>记录标识</summary><pre>'+esc(safeJSON({id:f.id,run_label:f.run_label,evidence_status:f.evidence_status,...(associated?{version_status:f.version_status}:{})}))+'</pre></details></details>';
      return '<article class="item-card" data-key="feedback:'+esc(f.id||f.feedback_id)+'"><div class="row"><h3>'+esc(memberName(f.member_id))+'</h3>'+pill(evidence,readback?'good':'')+'</div>'+metadata+facts+optional+detail+'<div class="item-actions">'+link(f.source_url,'查看反馈原文')+'</div></article>';
    }).join(''):empty('暂无相关反馈',state.projectId?'点击“更新反馈”读取该项目的飞书记录。':'进入项目后可更新该项目的飞书反馈。'));
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
    const data=workData(),items=list(data?.proposals);setText('proposal-count',items.filter(p=>p.status==='draft').length+' 待审阅');
    put('proposal-list',!data?unavailable():items.length?items.map(p=>'<article class="item-card" data-key="proposal:'+esc(p.id)+'"><div class="row"><h3>'+esc(p.title||proposalTarget(p))+'</h3>'+pill(p.publication_operation_id&&p.status!=='published'?'发布待核对':p.status==='draft'?'待审阅':p.target_kind==='template'&&p.status==='published'?'已生效':p.status,p.status==='draft'?'warn':'')+'</div><p>'+(!state.projectId?'<span class="small-note">'+(p.target_kind==='template'?'组织共用指令':esc(projectName(p.project_id)))+' · </span>':'')+proposalTargetDisplay(p)+'</p><div class="markdown-content">'+markdown(summary(p.summary,200))+'</div><details><summary>查看修改依据</summary>'+evidenceContent(p.evidence)+'</details>'+(p.rejection_reason?'<p>退回说明：'+esc(p.rejection_reason)+'</p>':'')+(p.status==='published'?'<p>'+(p.target_kind==='template'?'后续使用：':'采用效果：')+esc(p.adoption_status||'尚待后续反馈核对')+'</p>':'')+'<div class="item-actions"><button class="secondary" data-proposal="'+esc(p.id)+'" type="button">'+(p.status==='published'?'查看修改':'审阅草稿')+'</button></div></article>').join(''):empty('暂无方法建议','可与中央 Agent 讨论改进。'));
  }
  function renderPeople(){put('people-list',!state.overview?unavailable():allUsers().map(u=>'<article class="item-card" data-key="user:'+esc(u.id)+'"><div class="row"><div><h3>'+esc(displayName(u))+'</h3><small>'+esc(u.username)+'</small></div><button class="quiet" data-edit-user="'+esc(u.id)+'" type="button">编辑成员</button></div><div class="badge-row">'+list(u.roles).map(r=>pill(r)).join('')+(u.identity_mode==='role_test'?pill('角色测试','warn'):'')+'</div></article>').join('')||empty('暂无成员记录'));}
  function renderRuntime(){
    const r=state.overview?.runtime||{};
    put('runtime-state',!state.overview?unavailable():'<div class="badge-row">'+pill(r.paused?'中央 Agent 已暂停':'每五分钟检查',r.paused?'warn':'')+(r.schedule_verified?'':pill('尚未验证中央定时检查','warn'))+'</div><dl class="runtime-list"><dt>当前模型</dt><dd>'+esc(r.model_configured?(r.model||'已配置'):'尚未配置')+'</dd><dt>最近一次手动检查</dt><dd>'+date(r.last_manual)+'</dd><dt>最近一次定时检查</dt><dd>'+date(r.last_scheduled)+'</dd><dt>下次预计检查</dt><dd>'+(r.paused?'已暂停':date(r.next_due))+'</dd></dl>'+(r.error?'<p class="run-error">'+esc(readableError(r.error))+'</p>':'')+'<details><summary>运行预算与停机记录</summary><p>每次最多三分钟、六次模型调用和十二次工具调用。服务停止或电脑关闭时不会执行。错过检查：'+esc(r.missed??'暂无记录')+'。</p></details>');
    setText('toggle-runtime',r.paused?'恢复中央 Agent':'暂停中央 Agent');$('toggle-runtime').disabled=!state.overview;
  }
  function renderDiagnostics(){
    const d=state.diagnostics,o=d?.online||{},l=d?.local_agents||{};
    setText('diagnostics-status',state.diagnosticsLoading?(d?'正在更新，当前显示上次结果。':'正在检查运行记录…'):d?'观察时间：'+date(d.observed_at):'');
    errorAt('diagnostics-error',state.diagnosticsError);
    $('refresh-diagnostics').disabled=state.diagnosticsLoading;
    if(!d){put('diagnostics-result','');return;}
    const on=o.onboarding||{},m=o.model||{},t=o.tools||{},f=o.feishu||{},tasks=o.tasks||{},s=o.central_schedule||{};
    put('diagnostics-result','<p class="small-note">'+esc(d.scope||'本机已保存记录')+'</p>'+
      '<article class="item-card"><h3>接入与任务</h3><p>可开始工作 '+esc(on.ready_for_work??0)+' 人次；需要处理 '+esc(on.needs_attention??0)+' 人次。最近检查：'+date(on.last_checked_at)+'。</p><p>已知任务 '+esc(tasks.known??0)+' 项；待审阅草稿 '+esc(tasks.pending_drafts??0)+' 项。</p></article>'+
      '<article class="item-card"><h3>中央运行与飞书</h3><p>模型最近成功：'+date(m.last_success_at)+'；工具最近失败：'+date(t.last_failure_at)+'。</p><p>飞书最近读取：'+date(f.last_full_read_at)+'；待核对写入 '+esc(f.unresolved_operations??0)+' 项。</p><p>中央定时最近实际运行：'+date(s.last_actual_run_at)+'（'+esc(label(s.last_result||'未验证'))+'）。</p></article>'+
      '<article class="item-card"><h3>成员提交的证据</h3><p>已读回反馈 '+esc(l.feedback_readback_records??0)+' 条；最近成员报告：'+date(l.last_member_reported_at)+'。</p><p>报告真实定时触发 '+esc(l.reported_actual_scheduled_trigger??0)+' 人次。</p><p class="small-note">'+esc(l.evidence_boundary||'成员电脑上的执行情况须由后续反馈核对。')+'</p></article>'+
      '<details><summary>查看诊断限制</summary>'+list(d.limits).map(x=>'<p>'+esc(x)+'</p>').join('')+'</details>');
  }
  async function loadDiagnostics(){
    if(!admin()||state.diagnosticsLoading)return;
    const c={session:state.session},seq=++state.diagnosticsSeq;state.diagnosticsLoading=true;state.diagnosticsError='';renderDiagnostics();
    try{const data=await api('/diagnostics');if(!sameSession(c)||seq!==state.diagnosticsSeq)return;if(!data?.online||!data?.local_agents)throw new APIError('诊断结果不完整，请重新检查。');state.diagnostics=data;}
    catch(e){if(sameSession(c)&&seq===state.diagnosticsSeq)state.diagnosticsError=(state.diagnostics?'更新失败，当前显示上次结果。':'诊断暂时无法读取。')+' '+readableError(e?.message);}
    finally{if(sameSession(c)&&seq===state.diagnosticsSeq){state.diagnosticsLoading=false;renderDiagnostics();}}
  }
  function renderSources(){put('source-list',state.errors.sources&&!state.loaded.sources?empty('共享资料暂时无法读取','请点击刷新重试。'):state.sources.length?state.sources.map(s=>'<div class="source-item" data-key="source:'+esc(s.id)+'"><div class="row">'+(link(s.source_url,s.name||'飞书资料')||esc(s.name||'飞书资料'))+(s.removable?'<button class="quiet danger" data-remove-source="'+esc(s.id)+'" type="button">移除关联</button>':'')+'</div>'+(s.kind==='folder'||s.watch?'<p class="small-note">'+(s.kind==='folder'?'文件夹 · 仅目录':'检查正文变化')+'</p>':'')+'</div>').join(''):empty(state.loaded.sources?'尚未添加共享资料':'正在读取共享资料',state.loaded.sources?'点击“添加资料”，粘贴已授权的飞书链接。':''));}
  function runContent(run,brief=false){
    const failed=['failed','cancelled','interrupted','paused'].includes(run.status);
    const title=run.trigger==='scheduled'?'中央定时检查':run.kind==='sync'?'反馈更新':'对话处理';
    return '<div class="run-heading"><b>'+title+'</b>'+pill(run.status,failed?'error':running(run)?'warn':'')+'</div><p class="small-note">'+date(run.created_at)+'</p>'+(run.error?'<p class="run-error">'+esc(readableError(run.error))+'</p>':'')+(failed?'<button class="secondary" data-retry-run="'+esc(run.id)+'" type="button">继续这次处理</button>':'')+'<details><summary>查看执行详情</summary>'+(run.result?'<div class="markdown-content">'+markdown(textValue(run.result))+'</div>':'')+'<p>已记录 '+list(run.steps).length+' 次工具处理。</p>'+list(run.steps).map(step=>'<div class="tool-step">'+stepSources(step)+'<details><summary>工具与技术记录</summary><pre>'+esc(safeJSON(step))+'</pre></details></div>').join('')+'<p class="mono">'+esc(run.id)+'</p>'+(run.technical_detail?'<pre>'+esc(textValue(run.technical_detail))+'</pre>':'')+'</details>';
  }
  function renderRuns(){const runs=[...new Map([...list(state.overview?.runs),...Object.values(state.workspaces).flatMap(w=>list(w?.runs))].map(r=>[r.id,r])).values()].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')));put('run-list',runs.slice(0,20).map(run=>'<article class="item-card" data-key="run:'+esc(run.id)+'"><button class="quiet" data-run="'+esc(run.id)+'" type="button">'+date(run.created_at)+' · '+(run.project_id?esc(projectName(run.project_id))+' · ':'')+esc(run.kind==='sync'?'反馈更新':run.trigger==='scheduled'?'中央定时检查':'对话处理')+' · '+esc(label(run.status))+'</button></article>').join('')||empty('暂无运行记录'));put('chosen-run-detail',state.selectedRun?runContent(state.selectedRun):'');show('chosen-run-detail',!!state.selectedRun);}
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
  function renderProjects(){
    const instructionPanel=$('project-instruction');
    if(instructionPanel?.parentElement?.id==='project-list')$('page-projects').append(instructionPanel);
    put('project-list',state.errors.projects&&!state.loaded.projects?empty('项目暂时无法读取','请点击刷新重试。'):state.projects.length?state.projects.map(p=>'<article class="panel project-card" data-key="project:'+esc(p.id)+'"><div class="row"><h2>'+esc(p.name)+'</h2>'+(p.status==='failed'?pill('设置待修复','warn'):p.status&&p.status!=='active'?pill(p.status):'')+'</div>'+(p.description?'<p class="project-summary">'+esc(summary(p.description,125))+'</p>'+(p.description.length>125?'<details><summary>查看项目说明</summary><div class="markdown-content">'+markdown(p.description)+'</div></details>':''):'<p class="project-summary">暂无项目说明</p>')+'<p class="small-note">参与：'+esc(list(p.member_ids).map(id=>p.member_names?.[id]?String(p.member_names[id]).replace(/（本地角色测试）$/,''):member(id)?memberName(id):'其他参与成员').filter((v,i,a)=>a.indexOf(v)===i).join('、')||'尚未关联成员')+'</p>'+(p.folder_url||p.feedback_url?'<details><summary>项目资料与反馈位置</summary><div class="project-links">'+link(p.folder_url,'打开项目资料')+' '+link(p.feedback_url,'打开反馈原文')+'</div></details>':'')+(p.error||p.status==='failed'?'<div class="notice error">'+(admin()?esc(readableError(p.error||'项目设置尚未完成。'))+' 请点击“修复项目设置”继续。':'项目设置尚未完成，请联系管理员。')+'</div>':'')+'<div class="item-actions"><button class="primary" data-project-prompt="'+esc(p.id)+'" type="button"'+(p.status==='failed'||state.projectInstruction?.loading&&state.projectInstruction.project_id===p.id?' disabled':'')+(state.projectInstruction?.loading&&state.projectInstruction.project_id===p.id?' aria-busy="true"':'')+'>'+(state.projectInstruction?.loading&&state.projectInstruction.project_id===p.id?'正在准备…':'让我的 Agent 参与项目')+'</button>'+(admin()?'<button class="quiet" data-project-work="'+esc(p.id)+'" type="button">查看项目工作</button><button class="quiet" data-edit-project="'+esc(p.id)+'" type="button">'+(p.status==='failed'?'修复项目设置':'编辑项目')+'</button>':'')+'</div></article>').join(''):empty(state.loaded.projects?'暂无相关项目':'正在读取项目',state.loaded.projects?(admin()?'点击“创建项目”开始。':'请联系管理员将你加入相关项目。'):''));
    const i=state.projectInstruction;show('project-instruction',!!i&&!i.loading);
    if(i?.project_id&&!i.loading){const card=[...$('project-list').querySelectorAll('.project-card')].find(c=>c.dataset.key==='project:'+i.project_id);card?.after(instructionPanel);}
    if(i){const old=expired(i),needsPrepare=old||!!i.error||!i.prompt;
      setText('project-instruction-title',projectName(i.project_id)+' · 给 Agent 的说明');setText('project-prompt',needsPrepare?'':i.prompt);
      setText('project-copy-guidance',needsPrepare?'重新准备后，再复制给你的 Agent。':'复制后，粘贴到你的 Agent 对话中。');
      $('project-instruction-link').title=needsPrepare?'':'有效至 '+date(i.expires_at);
      setText('project-instruction-status',i.loading?'正在准备给 Agent 的说明…':i.error?i.error:old?'原说明已过期，请重新准备。':i.copied?'已复制，请粘贴到 Agent。':'');
      put('project-instruction-link',!needsPrepare&&i.url?link(i.url,'查看项目说明'):'');show('project-copy-details',!needsPrepare);
      setText('copy-project-instruction',needsPrepare?'重新准备项目说明':'复制给我的 Agent');$('copy-project-instruction').disabled=!!i.loading;
    }
  }
  async function selectProject(id,page=state.page){
    if(!state.projects.some(p=>p.id===id)){showError('该项目已不可访问，请刷新项目列表后重新选择。');renderNavigation();return;}
    if(id===state.projectId){state.page=page;render();return;}
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
        if(state.activeRun?.id===id){state.activeRun=run;renderActiveRun();renderConnection();}
        if(state.selectedRun?.id===id){state.selectedRun=run;renderRuns();}
        if(running(run))state.polling.set(id,setTimeout(poll,2000));
        else{state.polling.delete(id);await refresh();if(!sameSession(c))return;
          if(state.syncRun?.id===id){const target=state.syncRun.projectId,old=state.syncRun.before,now=feedbackSnapshot(target);const changed=now.filter(f=>old[f.id||f.feedback_id]!==feedbackVersion(f)).length;setText('sync-feedback-status',run.status==='succeeded'||run.status==='completed'?(state.errors.workspace&&state.projectId===target?'处理已完成，但反馈列表未刷新，请重试读取。':changed?'收到 '+changed+' 条新增或更新的反馈。':'暂无新反馈。'):'本次读取未完成，已有反馈保留。');errorAt('sync-feedback-error',run.error||'');state.syncRun=null;$('sync-feedback').disabled=!state.projectId;setText('sync-feedback','更新反馈');}
        }
      }catch(e){state.polling.delete(id);if(sameSession(c)){errorAt(state.syncRun?.id===id?'sync-feedback-error':'chat-error',e.message+' 运行状态尚未确认，请刷新核对。');if(state.syncRun?.id===id){$('sync-feedback').disabled=false;setText('sync-feedback','检查读取结果');}}}
    };await poll();
  }
  async function startRun(path,body,c=context()){
    const data=await api(path,{method:'POST',body:path==='/chat'?body:{...body,project_id:c.projectId}});const id=data.run_id||data.id;if(!id)throw new APIError('操作结果尚未确认，请先检查已有结果，再继续原操作。',0,'write_uncertain');
    if(!sameSession(c))return id;if(path==='/chat')state.activeRun={id,status:'queued',kind:'chat',trigger:'manual',project_id:''};renderActiveRun();renderConnection();if(path==='/chat')trackRun(id);return id;
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
  const recoveryKey=(kind,target='')=>['rdos-pending-write',state.user?.id,state.projectId,kind,target].join(':');
  function saveRecovery(kind,target,value){try{sessionStorage.setItem(recoveryKey(kind,target),JSON.stringify(value));}catch{}}
  function getRecovery(kind,target=''){try{return JSON.parse(sessionStorage.getItem(recoveryKey(kind,target))||'null');}catch{return null;}}
  function clearRecovery(kind,target=''){try{sessionStorage.removeItem(recoveryKey(kind,target));}catch{}}
  const projectFields=()=>({name:$('project-form-name').value.trim(),description:$('project-form-description').value.trim(),member_ids:[...$('project-members').querySelectorAll('input:checked')].map(i=>i.value),folder_url:$('project-folder-url').value.trim()});
  const projectFieldsKey=fields=>JSON.stringify([fields.name,fields.description,fields.member_ids,fields.folder_url]);
  function showTask(task=null,mode='manual'){
    state.task=task;state.taskMode=mode;state.taskConflict=false;state.taskOperation=task?.publication_operation_id||uid();state.taskPending=null;state.taskPayloadKey=null;$('task-form').reset();errorAt('task-form-error','');setText('task-form-status','');
    state.taskTargetId=task?.project_id||state.projectId||'';
    const chooseProject=!state.taskTargetId;
    setText('task-dialog-title',mode==='draft'?'审阅'+draftActionLabel(task):task?'编辑任务':'新建任务');setText('task-current-project',chooseProject?'':'发布到：'+projectName(state.taskTargetId));
    show('task-project-label',chooseProject);put('task-project','<option value="">请选择项目</option>'+state.projects.map(p=>'<option value="'+esc(p.id)+'">'+esc(p.name)+'</option>').join(''));$('task-project').value=state.taskTargetId;
    const action=mode==='draft'?(task?.draft_action||'create'):'',withdrawDraft=action==='withdraw';
    setText('task-dialog-description',mode==='draft'?(withdrawDraft?'确认后停用飞书中的这项任务；历史要求和反馈保留。':'确认后发布到飞书任务表。'):task?'将更新任务要求并生成新版本。':'发布到飞书任务表，供所选成员读取。');
    show('task-draft-impact',mode==='draft');put('task-draft-impact',mode==='draft'?'<p><b>'+esc(draftActionLabel(task))+'</b>'+(task?.base_task_version?' · 基于原版本 '+esc(task.base_task_version):'')+'</p><p>目标：'+esc(task?.task_id||'新任务')+'</p>':'');
    show('task-reason-label',mode==='draft');$('task-reason').value=task?.reason||'';
    $('task-id').value=task?.task_id||'';$('task-instructions').value=task?.instructions||'';$('task-related').value=task?.related_task||'';$('task-kind').value=task?.task_kind||'work';$('task-stage').value=task?.stage||'第一阶段';$('task-nature').value=task?.nature||'一次性';taskMembers(list(task?.member_ids));taskKind();
    const recovering=mode==='draft'&&!!task?.publication_operation_id;lockTask(recovering||withdrawDraft,recovering);setText('task-submit',recovering?'检查发布结果':mode==='draft'&&withdrawDraft?'确认停用':mode==='draft'&&action==='update'?'发布更新':task&&mode==='manual'?'发布更新':'发布任务');show('task-reject',mode==='draft'&&!recovering&&task?.status!=='published');
    if(recovering)setText('task-dialog-description','请先检查结果，再继续原发布。');
    const saved=getRecovery('task',task?.id||task?.task_id||'');
    if(saved){
      state.taskPending=saved;state.taskOperation=saved.body.operation_id;const f=mode==='draft'?saved.fields:saved.body;state.taskTargetId=f?.project_id||state.taskTargetId;
      if(state.taskTargetId){show('task-project-label',false);$('task-project').value=state.taskTargetId;setText('task-current-project','发布到：'+projectName(state.taskTargetId));}
      for(const [key,id] of Object.entries({instructions:'task-instructions',task_id:'task-id',related_task:'task-related',task_kind:'task-kind',stage:'task-stage',nature:'task-nature',reason:'task-reason'}))if(f?.[key]!==undefined)$(id).value=f[key];
      taskMembers(list(f?.member_ids));lockTask(true);setText('task-submit','继续原发布');setText('task-form-status','已恢复原内容，继续前将核对发布结果。');
    }
    rememberForm('task-dialog');open('task-dialog');
  }
  function showUser(user=null){
    state.editUser=user;$('user-form').reset();errorAt('user-form-error','');setText('user-form-status','');setText('user-dialog-title',user?'编辑成员':'新增成员');$('user-username').value=user?.username||'';$('user-username').disabled=!!user;$('user-display').value=user?.display_name||'';$('user-admin').checked=list(user?.roles).includes('admin');$('user-researcher').checked=user?list(user.roles).includes('researcher'):true;$('user-identity').value=user?.identity_mode||'actual';show('user-password-label',!user);rememberForm('user-dialog');open('user-dialog');
  }
  function showProject(project=null){
    state.editProject=project;state.projectOperation=uid();state.projectPayloadKey=null;state.projectPending=null;$('project-form').reset();$('project-form').querySelectorAll('input,textarea').forEach(i=>{i.readOnly=false;i.disabled=false;});errorAt('project-form-error','');setText('project-form-status','');setText('project-dialog-title',project?'编辑项目':'创建项目');$('project-form-name').value=project?.name||'';$('project-form-description').value=project?.description||'';$('project-folder-url').value=project?.folder_url||'';$('project-folder-url').required=!project?.feedback_id;$('project-members').innerHTML=memberOptions(list(project?.member_ids));setText('save-project',project?'保存项目':'创建项目');
    const saved=getRecovery('project',project?.id||'');
    if(saved?.body){
      state.projectPending=saved;state.projectOperation=saved.body.operation_id;state.projectPayloadKey=projectFieldsKey(saved.body);
      $('project-form-name').value=saved.body.name;$('project-form-description').value=saved.body.description;$('project-folder-url').value=saved.body.folder_url;$('project-members').innerHTML=memberOptions(saved.body.member_ids);
      if(!project?.id){$('project-form').querySelectorAll('input,textarea').forEach(i=>i.type==='checkbox'?i.disabled=true:i.readOnly=true);setText('save-project','检查创建结果');setText('project-form-status','上次创建结果还不清楚。先用原内容核对，确认后才能修改。');}
      else setText('project-form-status',project.feedback_setup&&!project.feedback_id?'上次输入已保留。保存时会先核对原反馈位置，再处理修改。':'上次保存结果尚未确认。再次提交会核对已有结果。');
    }else if(project?.feedback_setup&&!project.feedback_id){
      setText('project-dialog-title','修复项目设置');setText('project-form-status','反馈入口尚未完成。可以修改资料文件夹；保存时会先核对原反馈位置。');
      if(project.error)errorAt('project-form-error',readableError(project.error));
    }
    rememberForm('project-dialog');open('project-dialog');
  }
  function showProposal(item){
    state.proposal=item;state.publishOperation=item.publication_operation_id||uid();state.proposalEditing=false;
    if(state.proposalSourceChanged?.id!==item.id||state.proposalSourceChanged?.base_hash!==item.base_hash)state.proposalSourceChanged=null;
    errorAt('proposal-form-error',state.proposalSourceChanged?proposalStaleMessage(item):'');
    const editable=item.status!=='published'&&!item.publication_operation_id;
    delete $('proposal-review').dataset.rendered;put('proposal-review','<section class="proposal-purpose"><h3>修改目的</h3><div class="markdown-content">'+markdown(item.summary)+'</div>'+pill(item.status==='draft'?'待审阅':item.target_kind==='template'&&item.status==='published'?'已生效':item.status)+'</section><section class="proposal-target"><h3>'+(item.target_kind==='template'?'生效范围':'目标资料与影响')+'</h3><p>'+proposalTargetDisplay(item)+'</p><p>'+proposalImpact(item)+'</p></section><div id="proposal-preview" class="diff-grid"><section><h3>修改前</h3><div class="markdown-content">'+markdown(item.old_text||'追加新内容')+'</div></section><section><h3>修改后</h3><div class="markdown-content">'+markdown(item.new_text)+'</div></section></div><div id="proposal-editor" class="hidden stack-form"><label>修改目的<textarea aria-label="修改目的" id="proposal-summary" rows="3">'+esc(item.summary)+'</textarea></label><div class="diff-grid"><label>修改前<textarea aria-label="修改前" id="proposal-old" rows="9">'+esc(item.old_text)+'</textarea></label><label>修改后<textarea aria-label="修改后" id="proposal-new" rows="9">'+esc(item.new_text)+'</textarea></label></div><p id="proposal-unsaved" class="unsaved-notice"></p><button id="save-proposal" class="secondary" type="button">保存草稿</button></div><details><summary>查看依据</summary>'+evidenceContent(item.evidence)+'</details><details><summary>来源版本与记录标识</summary><p class="mono">'+esc(item.target_id)+'<br>'+esc(item.base_hash||'待核对')+'</p></details>'+(editable?'<div class="dialog-actions"><button id="reject-proposal" class="quiet danger" type="button">退回修改</button><button id="edit-proposal" class="secondary" type="button">编辑草稿</button><button id="publish-proposal" class="primary" type="button"'+(!item.draft_hash||item.status==='rejected'||state.proposalSourceChanged?' disabled':'')+'>确认发布</button></div>':'<div class="dialog-actions"><button class="secondary" data-close="proposal-dialog" type="button">关闭</button>'+(item.status!=='published'&&item.publication_operation_id?'<button id="publish-proposal" class="primary" type="button">检查发布结果</button>':'')+'</div>'));
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
  $('login-form').addEventListener('submit',async e=>{
    e.preventDefault();const b=e.submitter;if(b.disabled)return;b.disabled=true;b.setAttribute('aria-busy','true');b.textContent='正在登录…';setText('login-error','');
    try{const data=await api('/auth/login',{method:'POST',body:{username:$('login-username').value.trim(),password:$('login-password').value}});$('login-password').value='';beginSession(data.user||data);await refresh();}
    catch(error){setText('login-error',error.message);}finally{b.disabled=false;b.removeAttribute('aria-busy');b.textContent='登录';}
  });
  $('logout').addEventListener('click',()=>busy($('logout'),async()=>{await api('/auth/logout',{method:'POST',body:{}});resetSession();}));
  $('refresh-workspace').addEventListener('click',()=>busy($('refresh-workspace'),async()=>{const result=await refresh();if(!state.user)return;toast(result?.ok?'已更新。':'部分内容未更新：'+(result?.failures||['当前页面']).join('、')+'。已有内容保留。',!result?.ok);},'workspace-error'));
  $('work-show-all').addEventListener('click',()=>{clearProject();state.projectId=null;state.page='work';render();refresh();});
  $('open-drawer').addEventListener('click',()=>{state.selectedRun=null;renderRuns();open('workspace-drawer');});
  $('choose-new').addEventListener('click',()=>choosePath('new'));$('choose-refresh').addEventListener('click',()=>choosePath('refresh'));
  $('change-onboarding-path').addEventListener('click',()=>{state.reentering=true;renderOnboarding();});
  $('prepare-onboarding-instruction').addEventListener('click',requestOnboardingInstruction);
  $('continue-onboarding').addEventListener('click',requestOnboardingInstruction);
  $('check-onboarding').addEventListener('click',()=>checkOnboarding(true));$('copy-onboarding-instruction').addEventListener('click',()=>expired(state.instruction)?requestOnboardingInstruction():copyInstruction('onboarding'));
  $('onboarding-go-project').addEventListener('click',()=>{state.page='projects';render();});
  $('open-setup').addEventListener('click',()=>busy($('open-setup'),async()=>{const c=context(),data=await api('/onboarding/setup',{text:true});if(!current(c))return;put('setup-content',markdown(data));open('setup-dialog');},'onboarding-action-error'));
  $('new-project').addEventListener('click',()=>showProject());
  $('project-form').addEventListener('submit',async e=>{
    e.preventDefault();if(state.forms.get('project-dialog')?.busy)return;
    const target=state.editProject,targetId=target?.id||'',fields=projectFields();if(!fields.member_ids.length){errorAt('project-form-error','请至少选择一位参与成员。');return;}
    const inputKey=projectFieldsKey(fields);
    // Reuse an operation only for exactly the same target and content. A changed
    // folder must get a new ID; the server reconciles the old remote write first.
    if(state.projectPayloadKey&&state.projectPayloadKey!==inputKey)state.projectOperation=uid();
    state.projectPayloadKey=inputKey;
    const body=state.projectPending&&!targetId?state.projectPending.body:{...fields,operation_id:state.projectOperation};
    await formAction('project-dialog','project-form-error',async c=>{
      setText('project-form-status','正在核对反馈入口并保存项目…');
      try{
        saveRecovery('project',targetId,{body});
        await api(targetId?'/projects/'+encodeURIComponent(targetId):'/projects',{method:targetId?'PATCH':'POST',body,timeout_ms:180000});
        if(!current(c))return;
        clearRecovery('project',targetId);state.projectPending=null;close('project-dialog',true);await refresh();toast('项目已保存，反馈入口已检查。');
      }catch(error){
        if(!current(c))return;
        await refresh();if(!current(c))return;
        if(error.projectId){
          // The server has an actual project record. Continue against that
          // record, with a fresh operation if the first request was a create.
          const failed=state.projects.find(p=>p.id===error.projectId);
          state.editProject=failed||{id:error.projectId,...fields};
          clearRecovery('project',targetId);state.projectPending=null;
          if(!targetId){state.projectOperation=uid();state.projectPayloadKey=inputKey;}
          setText('project-dialog-title','修复项目设置');setText('save-project','保存项目');
          setText('project-form-status',failed?.feedback_setup&&!failed.feedback_id?'输入已保留。保存时会先核对原反馈位置，再处理当前修改。':'输入已保留，修正后可再次保存。');
        }else if(uncertain(error)){
          state.projectPending={body,targetId};
          if(!targetId){$('project-form').querySelectorAll('input,textarea').forEach(i=>i.type==='checkbox'?i.disabled=true:i.readOnly=true);setText('save-project','检查创建结果');setText('project-form-status','创建结果尚未确认，请先用原内容核对。');}
          else setText('project-form-status','结果尚未确认。可修改输入；保存时会先核对原反馈位置。');
        }else{
          clearRecovery('project',targetId);state.projectPending=null;
          setText('project-form-status','输入已保留，修正后可再次保存。');
        }
        throw error;
      }
    });
  });
  $('copy-project-instruction').addEventListener('click',()=>{const i=state.projectInstruction;if(i&&(expired(i)||i.error||!i.prompt))prepareProject(i.project_id);else copyInstruction('project');});
  $('chat-form').addEventListener('submit',async e=>{
    e.preventDefault();const raw=$('chat-message').value,message=raw.trim(),c=context();if(!message||!admin()||$('chat-send').disabled)return;
    state.chatSubmitting=true;errorAt('chat-error','');setText('chat-status','正在提交这条消息…');const operation=state.chatOperation?.message===message?state.chatOperation:{message,id:uid()};state.chatOperation=operation;renderConnection();
    try{await startRun('/chat',{message,operation_id:operation.id},c);if(!sameSession(c))return;if($('chat-message').value===raw)$('chat-message').value='';state.chatDrafts.organization=$('chat-message').value;state.chatOperation=null;setText('chat-status','消息已提交，中央 Agent 正在处理。');await refresh();}
    catch(error){if(sameSession(c)){errorAt('chat-error',error);setText('chat-status','本次输入保留。重新提交同一条消息会沿用原操作。');}}
    finally{if(sameSession(c)){state.chatSubmitting=false;renderConnection();}}
  });
  $('chat-message').addEventListener('input',()=>{state.chatDrafts.organization=$('chat-message').value;});
  $('chat-message').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)&&!$('chat-send').disabled){e.preventDefault();$('chat-form').requestSubmit();}});
  async function syncFeedback(){
    const c=context();if(!c.projectId){errorAt('sync-feedback-error','请先从项目页进入要更新的项目。');return;}if(state.syncRun){trackRun(state.syncRun.id);return;}$('sync-feedback').disabled=true;setText('sync-feedback','正在读取…');setText('sync-feedback-status','正在读取 '+projectName(c.projectId)+' 的反馈…');errorAt('sync-feedback-error','');
    try{const before=Object.fromEntries(feedbackSnapshot(c.projectId).map(f=>[f.id||f.feedback_id,feedbackVersion(f)]));state.syncOperation=state.syncOperation||uid();const id=await startRun('/sync',{operation_id:state.syncOperation},c);if(!sameSession(c))return;state.syncOperation=null;state.syncRun={id,before,projectId:c.projectId};trackRun(id);}
    catch(e){if(sameSession(c)){errorAt('sync-feedback-error',e);setText('sync-feedback-status','已有反馈保留。');$('sync-feedback').disabled=!state.projectId;setText('sync-feedback','更新反馈');}}
  }
  $('sync-feedback').addEventListener('click',syncFeedback);$('manual-run').addEventListener('click',syncFeedback);
  $('toggle-runtime').addEventListener('click',()=>busy($('toggle-runtime'),async()=>{const c=context(),paused=!state.overview?.runtime?.paused;await api('/runtime',{method:'PATCH',body:{paused}});if(!sameSession(c))return;await refresh();setText('runtime-error',paused?'中央 Agent 与周期检查已暂停，外部 Local Agent 仍按各自安排运行。':'中央 Agent 已恢复运行。');},'runtime-error'));
  $('diagnostics-details').addEventListener('toggle',()=>{if($('diagnostics-details').open&&!state.diagnostics)loadDiagnostics();});
  $('refresh-diagnostics').addEventListener('click',loadDiagnostics);
  $('new-task').addEventListener('click',()=>showTask());$('task-kind').addEventListener('change',taskKind);
  $('task-project').addEventListener('change',()=>{state.taskTargetId=$('task-project').value;taskMembers();errorAt('task-form-error','');});
  $('task-form').addEventListener('submit',async e=>{
    e.preventDefault();const fields=taskFields(),targetProject=state.taskTargetId;if(!targetProject||!state.projects.some(p=>p.id===targetProject)){errorAt('task-form-error','请先选择任务所属项目。');return;}if(!fields.member_ids.length){errorAt('task-form-error','请至少选择一位适用成员。');return;}
    if(state.taskMode==='draft'&&['update','withdraw'].includes(state.task?.draft_action)&&!fields.reason){errorAt('task-form-error','请说明修改或停用的理由。');$('task-reason').focus();return;}
    const payloadKey=JSON.stringify(fields);if(!state.taskPending&&state.taskPayloadKey&&state.taskPayloadKey!==payloadKey)state.taskOperation=uid();state.taskPayloadKey=payloadKey;const item=state.task,mode=state.taskMode,op=state.taskOperation;
    await formAction('task-dialog','task-form-error',async c=>{
      setText('task-form-status',state.taskConflict?'正在核对最新任务并保存草稿…':state.taskPending||item?.publication_operation_id?'正在检查原发布结果…':'正在发布，请等待远端核对…');
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
        if(!current(c))return;clearRecovery('task',item?.id||item?.task_id||'');state.taskPending=null;close('task-dialog',true);await refresh();toast(mode==='draft'&&item?.draft_action==='withdraw'?'任务已停用，历史记录保留。':'任务已写入飞书并核对。');
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
  $('add-user').addEventListener('click',()=>showUser());
  $('user-form').addEventListener('submit',async e=>{
    e.preventDefault();const roles=[$('user-admin').checked&&'admin',$('user-researcher').checked&&'researcher'].filter(Boolean);if(!roles.length){errorAt('user-form-error','请至少保留一种角色。');return;}
    const u=state.editUser,body={display_name:$('user-display').value.trim(),roles,identity_mode:$('user-identity').value};if(!u){body.username=$('user-username').value.trim();if($('user-password').value)body.password=$('user-password').value;}
    await formAction('user-dialog','user-form-error',async c=>{setText('user-form-status','正在保存成员…');const data=await api(u?'/users/'+encodeURIComponent(u.id):'/users',{method:u?'PATCH':'POST',body});if(!sameSession(c))return;$('user-password').value='';close('user-dialog',true);await refresh();if(!sameSession(c))return;
      if(data.password){$('credential-username').value=data.user?.username||body.username;$('credential-password').value=data.password;setText('credential-close-warning','');open('credential-dialog');}else toast('成员信息已保存。');
    });
  });
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
    if(b.dataset.page){if(!admin()&&['agent','work','global'].includes(b.dataset.page))return;if(b.dataset.page==='work'&&b.classList.contains('page-tab')&&state.projectId){clearProject();state.projectId=null;}state.page=b.dataset.page;render();maybeCheck();}
    if(b.dataset.tab){state.tab=b.dataset.tab;document.querySelectorAll('.tab').forEach(t=>{t.classList.toggle('active',t.dataset.tab===state.tab);t.setAttribute('aria-selected',String(t.dataset.tab===state.tab));});document.querySelectorAll('.tab-view').forEach(v=>v.classList.toggle('hidden',v.id!=='view-'+state.tab));}
    if(b.dataset.chatExample){$('chat-message').value=b.dataset.chatExample;$('chat-message').focus();}
    if(b.dataset.projectPrompt)prepareProject(b.dataset.projectPrompt);
    if(b.dataset.projectWork)selectProject(b.dataset.projectWork,'work');
    if(b.dataset.editProject)showProject(state.projects.find(p=>p.id===b.dataset.editProject));
    if(b.dataset.editTask)showTask(list(workData()?.tasks).find(t=>String(t.task_id)===b.dataset.editTask));
    if(b.dataset.taskDraft)showTask(list(workData()?.task_drafts).find(t=>String(t.id)===b.dataset.taskDraft),'draft');
    if(b.dataset.withdrawTask){const t=list(workData()?.tasks).find(t=>String(t.task_id)===b.dataset.withdrawTask),op=uid(),c=context();if(!t?.project_id){errorAt('sync-feedback-error','无法确认这项任务所属项目，请刷新后重试。');return;}confirm('撤回这项任务？','任务将标记为已撤回，历史要求与反馈保留。','撤回任务',async()=>{await api('/tasks/'+encodeURIComponent(t.task_id)+'/withdraw',{method:'POST',body:{operation_id:op,project_id:t.project_id}});if(sameSession(c)){await refresh();toast('任务已撤回，历史记录保留。');}});}
    if(b.dataset.rejectTask)rejectTask(list(workData()?.task_drafts).find(t=>t.id===b.dataset.rejectTask));
    if(b.dataset.editUser)showUser(allUsers().find(u=>String(u.id)===b.dataset.editUser));
    if(b.dataset.proposal)showProposal(list(workData()?.proposals).find(p=>String(p.id)===b.dataset.proposal));
    if(b.dataset.removeSource){const id=b.dataset.removeSource,c=context();confirm('移除资料关联？','移除此资料入口，不删除飞书原文。','移除关联',async()=>{await api('/sources/'+encodeURIComponent(id),{method:'DELETE'});if(sameSession(c)){await refresh();toast('关联已移除，飞书原文保留。');}});}
    if(b.dataset.run)busy(b,async()=>{const c=context(),id=b.dataset.run;state.runViewSeq=(state.runViewSeq||0)+1;const seq=state.runViewSeq;const data=await api('/runs/'+encodeURIComponent(id));if(!current(c)||seq!==state.runViewSeq)return;state.selectedRun=data.run||data.item||data;renderRuns();open('workspace-drawer');$('workspace-drawer').scrollTop=0;},'chat-error');
    if(b.dataset.retryRun)busy(b,async()=>{const c=context(),id=b.dataset.retryRun;const data=await api('/runs/'+encodeURIComponent(id)+'/retry',{method:'POST',body:{}});if(!current(c))return;const next=data.run_id||data.id||data.item?.id||id;state.activeRun={id:next,status:'queued',kind:'chat'};renderActiveRun();renderConnection();trackRun(next);await refresh();},'chat-error');
    if(b.id==='edit-proposal'){state.proposalEditing=!state.proposalEditing;show('proposal-editor',state.proposalEditing);show('proposal-preview',!state.proposalEditing);b.textContent=state.proposalEditing?'查看草稿预览':'编辑草稿';if(!state.proposalEditing&&proposalChanged())put('proposal-preview','<section><h3>修改前</h3><div class="markdown-content">'+markdown($('proposal-old').value)+'</div></section><section><h3>修改后（未保存）</h3><div class="markdown-content">'+markdown($('proposal-new').value)+'</div></section>');updatePublishButton();}
    if(b.id==='save-proposal'){const id=state.proposal.id,body={old_text:$('proposal-old').value,new_text:$('proposal-new').value,summary:$('proposal-summary').value};formAction('proposal-dialog','proposal-form-error',async c=>{const data=await api('/proposals/'+encodeURIComponent(id)+'/edit',{method:'POST',body});if(!current(c))return;showProposal(data.item||data);await refresh();toast('草稿已保存，请审阅后确认发布。');});}
    if(b.id==='reject-proposal'){const id=state.proposal.id,c=context();confirm('退回方法建议？','本次修改不会发布，可说明需要调整的内容。','退回修改',async reason=>{await api('/proposals/'+encodeURIComponent(id)+'/reject',{method:'POST',body:{reason}});if(current(c)){close('proposal-dialog',true);await refresh();toast('方法建议已退回。');}},true);}
    if(b.id==='publish-proposal'){if(state.proposalSourceChanged)return;const item=state.proposal,op=state.publishOperation;formAction('proposal-dialog','proposal-form-error',async c=>{if(proposalChanged()||!item?.draft_hash)throw new APIError('请先保存草稿修改，再确认发布。');try{await api('/proposals/'+encodeURIComponent(item.id)+'/publish',{method:'POST',body:{expected_hash:item.draft_hash,operation_id:op},timeout_ms:180000});if(!current(c))return;close('proposal-dialog',true);await refresh();toast(item.target_kind==='template'?'新生成的指令已生效；成员读取和使用仍待反馈。':'修改已发布，采用效果仍待后续反馈。');}catch(e){if(current(c)){if(['source_changed','patch_conflict'].includes(e.code)){state.proposalSourceChanged={id:item.id,base_hash:item.base_hash};throw new APIError(proposalStaleMessage(item),e.status,e.code,e.technical);}await refresh();const saved=list(workData()?.proposals).find(p=>p.id===item.id);if(uncertain(e)||saved?.publication_operation_id){state.proposal=saved?.publication_operation_id?saved:{...item,publication_operation_id:op};showProposal(state.proposal);}}throw e;}});}
  });
  $('proposal-dialog').addEventListener('input',updatePublishButton);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.user){refresh();maybeCheck();}});window.addEventListener('focus',maybeCheck);
  document.querySelectorAll('dialog').forEach(dialog=>{
    dialog.addEventListener('cancel',e=>{e.preventDefault();close(dialog.id);});
    dialog.addEventListener('click',e=>{if(e.target!==dialog)return;const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)close(dialog.id);});
  });
  window.addEventListener('beforeunload',e=>{if([...state.forms.entries()].some(([id,f])=>$(id).open&&(f.busy||formValue(id)!==f.initial))||$('chat-message').value||$('credential-dialog').open){e.preventDefault();e.returnValue='';}});
  (async()=>{try{const data=await api('/auth/me');beginSession(data.user||data);await refresh();}catch(e){if(e.status!==401)setText('login-error',e.message);}})();
})();
