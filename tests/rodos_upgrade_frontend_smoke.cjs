/* Original Panel UI in Chrome. Every API is an isolated in-memory fixture. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('/Users/alexliu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'..'),base=path.resolve(root,'../evidence/rodos_upgrade_ui');
const dir=fs.existsSync(base)?path.join(base,'run_'+new Date().toISOString().replace(/[:.]/g,'-')):base;
fs.mkdirSync(dir,{recursive:true,mode:0o700});
const report={mode:'Original UI in Chrome; isolated API fixtures only; no live model, Feishu or node writes',
  started_at:new Date().toISOString(),checks:[],errors:[],external_requests:[],unknown_requests:[]};
const pass=name=>{report.checks.push({name,passed:true});console.log('PASS '+name);};
const clone=value=>JSON.parse(JSON.stringify(value));
const clock='2026-10-24T02:00:00Z';
let browser,page;
(async()=>{
 browser=await chromium.launch({channel:'chrome',headless:true,args:['--disable-background-networking','--no-first-run']});
 page=await browser.newPage({viewport:{width:1440,height:1100},acceptDownloads:true});page.setDefaultTimeout(10000);
 page.on('pageerror',error=>report.errors.push(error.message));
 page.on('dialog',dialog=>dialog.accept());
 const admin={id:'member_admin',username:'admin',display_name:'管理员',roles:['admin'],enabled:true};
 const node={node_id:'node_workbuddy',public_id:'node_workbuddy',member_id:'member_workbuddy',display_name:'WorkBuddy',enabled:true};
 const researcher={id:'member_workbuddy',username:'workbuddy',display_name:'研发成员',roles:['researcher'],enabled:true,nodes:[node]};
 const project={id:'project_rdos',name:'RDOS 建设',is_default:true,status:'active',node_ids:['node_workbuddy'],member_ids:[researcher.id]};
 const thread={id:'thread_current',project_id:project.id,title:'当前工作',created_at:clock};
 let policy={version:2,content:'先说明工作结果、问题和必要决定。',hash:'a'.repeat(64),updated_at:clock,updated_by:admin.id,file_updated:true};
 let conflictPolicy=true,chatFail=false;
 const memories=[{id:'memory_existing',topic:'人的汇报',content:'先说明实际结果。',scope:'organization',project_id:'',
  applicability:{kind:'organization'},version:1,status:'active',evidence_kind:'explicit',
  sources:[{source_kind:'panel_settings',user_id:admin.id}],updated_at:clock}];
 const chatRuns=[],messages=[],maintenanceRuns=[{id:'maintenance_prior',kind:'inspection',status:'succeeded',
  project_ids:[project.id],created_at:clock,completed_at:clock,summary:'上次已完成同步与只读检查。',
  inspection:{observed_at:clock,project_ids:[project.id],issues:[],projects:[],read_only:true}}];
 const schedules=[{id:'hourly_refresh',enabled:true,revision:1,timezone:'Asia/Shanghai',next_due_at:'2026-10-24T03:00:00Z',description:'每小时整点'},
  {id:'weekly_inspection',enabled:true,revision:1,timezone:'Asia/Shanghai',next_due_at:'2026-10-30T15:00:00Z',description:'每周五23:00'}];
 let bound=false,downloads=0;const posted=[];
 page.on('download',()=>downloads++);
 const enrollment={request_id:'enroll_fixture_workbuddy',request_hash:'c'.repeat(64),node_id:'node_workbuddy',
  task_id:'task_fixture_maintenance',requirements_version:1,confirmation_code:'482731',requested_at:clock,status:'pending'};
 const preview=()=>['# 中央共享记忆摘要',...memories.filter(m=>m.status==='active').map(m=>'- '+m.topic+'：'+m.content)].join('\n');
 const active=()=>maintenanceRuns.find(run=>['queued','running','draining'].includes(run.status))||null;
 const maintenanceState=()=>({schedules:clone(schedules),active_run:clone(active()),paused:false,heartbeat_running:true,
  latest_inspection:clone(maintenanceRuns.find(run=>run.kind==='inspection'&&run.inspection)||null)});
 const wait=async test=>{for(let n=0;n<120;n++){if(test())return;await new Promise(resolve=>setTimeout(resolve,25));}throw Error('API fixture condition timed out');};
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),p=url.pathname,method=request.method();
  const send=(value,status=200,type='application/json')=>route.fulfill({status,contentType:type,body:typeof value==='string'?value:JSON.stringify(value)});
  if(url.hostname!=='rdos.test'){report.external_requests.push(request.url());return route.abort();}
  if(p==='/')return send(fs.readFileSync(path.join(root,'server/static/central.html'),'utf8'),200,'text/html');
  if(/central\.(?:js|css)$/.test(p))return send(fs.readFileSync(path.join(root,'server/static',path.basename(p)),'utf8'),200,p.endsWith('.js')?'text/javascript':'text/css');
  if(p==='/api/lab/info')return send({env_id:'fixture_rodos',generation:'fixture_version',project_id:'lab_project_phase0',runtime:{model_mode:'fixture'}});
  if(p==='/api/central/auth/me'||p==='/api/central/auth/login')return send({user:admin});
  if(p==='/api/central/projects')return send({items:[project],nodes:[node],default_folder_url:'https://rdos.test/drive/folder/project'});
  if(p==='/api/central/users')return send({items:[admin,researcher]});
  if(p==='/api/central/threads')return send({items:[thread]});
  if(p==='/api/central/workspace'||p==='/api/central/overview')return send({user:admin,users:[admin,researcher],project,
    messages:clone(messages),runs:clone(chatRuns),tasks:[],runtime:{paused:false}});
  if(p==='/api/central/chat'){
   const body=request.postDataJSON();posted.push({path:p,body});
   if(chatFail)return send({detail:{code:'fixture_failure',message:'这次未能提交，原文已保留。'}},503);
   const run={id:'chat_'+(chatRuns.length+1),status:'running',kind:'chat',trigger:'manual',steps:[],project_id:project.id,
    thread_id:thread.id,created_at:clock,operation_id:body.operation_id};chatRuns.push(run);
   messages.push({id:run.id+':user',role:'user',run_id:run.id,user_id:admin.id,thread_id:thread.id,project_id:project.id,
    operation_id:body.operation_id,content:body.message,created_at:clock});
   return send({run_id:run.id});
  }
  if(p.startsWith('/api/central/runs/'))return send({run:clone(chatRuns.find(run=>run.id===p.split('/').at(-1)))});
  if(p==='/api/central/settings/agent'){
   if(method==='GET')return send(clone(policy));
   const body=request.postDataJSON();posted.push({path:p,body});
   if(conflictPolicy){conflictPolicy=false;policy={...policy,version:3,content:'另一位管理员刚保存的职责。'};
    return send({detail:{code:'policy_conflict',message:'职责已被更新，请核对当前版本后再保存。',current_version:3,current:clone(policy)}},409);}
   if(body.expected_version!==policy.version)return send({detail:{code:'policy_conflict',message:'职责版本冲突。',current_version:policy.version,current:clone(policy)}},409);
   policy={...policy,version:policy.version+1,content:body.content};return send({...policy,saved:true,reused:false});
  }
  if(p==='/api/central/settings/memories'){
   if(method==='GET')return send({items:clone(memories),has_more:false});
   const body=request.postDataJSON();posted.push({path:p,body});let item=memories.find(m=>m.id===body.memory_id);
   if(body.operation==='remember'){
    item={id:'memory_added',topic:body.topic,content:body.content,scope:body.scope,project_id:body.project_id||'',
     applicability:{kind:body.scope},version:1,status:'active',evidence_kind:'explicit',sources:[{source_kind:'panel_settings',user_id:admin.id}]};
    memories.push(item);
   }else{
    assert(item,'Memory fixture requires an existing ID');assert.equal(body.expected_version,item.version);
    item.version++;
    if(body.operation==='forget'){item.status='forgotten';item.content='';item.topic='';item.sources=[];}
    else if(body.operation==='invalidate')item.status='inactive';
    else{item.content=body.content;item.status='active';}
   }
   return send({ok:true,saved:true,changes:[{kind:body.operation,id:item.id,version:item.version,status:item.status}],summary_updated:true});
  }
  if(p==='/api/central/settings/memory-preview')return send({available:true,content:preview(),hash:'b'.repeat(64),summary_updated_at:clock,generated:true});
  if(p==='/api/central/maintenance/state')return send(maintenanceState());
  if(p==='/api/central/maintenance/runs')return send({items:clone(maintenanceRuns),total:maintenanceRuns.length});
  if(p.startsWith('/api/central/maintenance/runs/'))return send(clone(maintenanceRuns.find(run=>run.id===p.split('/').at(-1))));
  if(p==='/api/central/maintenance/refresh'||p==='/api/central/maintenance/inspect'){
   const body=request.postDataJSON();posted.push({path:p,body});const run={id:'maintenance_'+maintenanceRuns.length,
    kind:p.endsWith('/inspect')?'inspection':'refresh',status:'queued',project_ids:[project.id],created_at:clock,
    summary:'已启动，尚未完成。',results:[]};maintenanceRuns.unshift(run);return send(clone(run));
  }
  if(p==='/api/central/maintenance/schedule'){
   const body=request.postDataJSON();posted.push({path:p,body});const item=schedules.find(s=>s.id===body.schedule_id);
   assert.equal(body.expected_revision,item.revision);item.revision++;item.enabled=body.enabled;return send(clone(item));
  }
  if(p==='/api/central/peer/nodes')return send({nodes:[{node_id:'node_workbuddy',label:'WorkBuddy',bound,ready:false,
    binding_id:bound?'binding_fixture':null,skill_version:null,pending_requests:bound?[]:[clone(enrollment)]}]});
  if(p==='/api/central/peer/nodes/node_workbuddy/approve-enrollment'){
   const body=request.postDataJSON();posted.push({path:p,body});bound=true;
   assert.equal(body.request_id,enrollment.request_id);assert.equal(body.expected_request_hash,enrollment.request_hash);assert.equal(body.rotate,false);
   return send({node_id:'node_workbuddy',binding_id:'binding_fixture',bound:true,ready:false,registry_event_id:'fixture_registry'});
  }
  if(p.startsWith('/api/central/')){report.unknown_requests.push(method+' '+p);return send({items:[]});}
  return send('',404,'text/plain');
 });
 await page.goto('http://rdos.test/');await page.waitForSelector('#app-view:not(.hidden)');
 await page.click('[data-page="agent"]');await page.waitForSelector('#thread-select option[value="thread_current"]',{state:'attached'});
 assert.equal((await page.locator('[data-page="agent"]').innerText()).trim(),'中央Agent');
 assert.equal((await page.locator('#page-title').innerText()).trim(),'中央Agent');
 await page.fill('#chat-message','请核对当前工作');await page.press('#chat-message','Enter');await wait(()=>chatRuns.length===1);
 await page.waitForSelector('#messages .thinking');
 assert.equal((await page.locator('#messages .thinking .message-name').innerText()).trim(),'Rodos_中央Agent');
 assert.match(await page.locator('#messages .thinking').innerText(),/思考中/);
 assert.match(await page.locator('#messages .user').innerText(),/请核对当前工作/);
 pass('Navigation and title stay 中央Agent; the actual pending reply is signed Rodos_中央Agent');
 await page.screenshot({path:path.join(dir,'chat-thinking.png'),fullPage:true});
 chatRuns[0].status='succeeded';messages.push({id:'chat_1:assistant',role:'assistant',content:'已核对，当前工作记录齐全。',
  project_id:project.id,thread_id:thread.id,run_id:'chat_1',created_at:clock});
 await page.waitForFunction(()=>document.querySelector('#messages').textContent.includes('当前工作记录齐全'));
 assert.equal((await page.locator('#messages .agent:not(.thinking) .message-name').innerText()).trim(),'Rodos_中央Agent');
 chatFail=true;await page.fill('#chat-message','失败时仍保留我的内容');await page.press('#chat-message','Enter');
 await page.waitForSelector('#messages .chat-failure');
 assert.equal((await page.locator('#messages .chat-failure .message-name').innerText()).trim(),'Rodos_中央Agent');
 pass('Completed replies and submission failure messages retain the Rodos_中央Agent name');
 await page.click('[data-page="global"]');await page.waitForFunction(()=>document.querySelector('#rodos-policy-content').value.includes('先说明工作结果'));
 assert.equal(await page.locator('#rodos-agent-tab').innerText(),'Agent.md');
 assert.equal(await page.locator('#rodos-memory-tab').innerText(),'Memory.md');
 await page.fill('#rodos-policy-content','我的未保存职责改动必须保留。');await page.click('#rodos-policy-save');
 await page.waitForFunction(()=>document.querySelector('#rodos-policy-error').textContent.length>0);
 assert.equal(await page.locator('#rodos-policy-content').inputValue(),'我的未保存职责改动必须保留。');
 assert.match(await page.locator('#rodos-policy-error').innerText(),/更新|版本|变化/);
 pass('Settings expose Agent.md and Memory.md; stale Agent versions keep the administrator draft');
 await page.waitForSelector('[data-policy-rebase]');
 assert.match(await page.locator('#rodos-policy-conflict').innerText(),/另一位管理员刚保存的职责/);
 await page.click('[data-policy-rebase]');
 assert.equal(await page.locator('#rodos-policy-content').inputValue(),'我的未保存职责改动必须保留。');
 await page.fill('#rodos-policy-content','先说明结果；需要决定时再展开。');await page.click('#rodos-policy-save');
 await wait(()=>policy.version===4);await page.waitForFunction(()=>document.querySelector('#rodos-policy-status').textContent.length>0);
 assert.match(await page.locator('#rodos-policy-meta').innerText(),/4/);
 pass('A fresh Agent revision saves through the settings endpoint and displays its effective version');
 await page.click('#rodos-memory-tab');await page.waitForSelector('[data-memory-edit="memory_existing"]');
 assert.equal(await page.locator('#rodos-memory-tab').getAttribute('aria-selected'),'true');
 await page.click('#rodos-memory-add');await page.fill('#rodos-memory-topic','机器交互');await page.fill('#rodos-memory-content','内容完整，去掉重复交代。');await page.click('#rodos-memory-save');
 await page.waitForSelector('[data-memory-edit="memory_added"]');
 assert.match(await page.locator('#rodos-memory-preview').textContent(),/内容完整，去掉重复交代/);
 const add=posted.find(p=>p.path.endsWith('/memories')&&p.body.operation==='remember');assert.equal(add.body.expected_version,0);
 pass('Memory creation is a topic-based settings action and regenerates the Memory.md preview');
 await page.click('[data-memory-edit="memory_added"]');await page.fill('#rodos-memory-content','内容完整，流程说明不重复。');await page.click('#rodos-memory-save');
 await wait(()=>memories.find(m=>m.id==='memory_added').version===2);
 await page.waitForFunction(()=>document.querySelector('#rodos-memory-preview').textContent.includes('流程说明不重复'));
 const correction=posted.find(p=>p.path.endsWith('/memories')&&['correct','replace'].includes(p.body.operation));assert.equal(correction.body.expected_version,1);
 pass('Memory corrections reuse the selected topic and bind its previous version');
 await page.click('[data-memory-forget="memory_added"]');
 await page.waitForSelector('#confirm-dialog[open]');await page.click('#confirm-action');
 await wait(()=>memories.find(m=>m.id==='memory_added').status==='forgotten');
 await page.waitForFunction(()=>!document.querySelector('#rodos-memory-preview').textContent.includes('流程说明不重复'));
 assert.doesNotMatch(await page.locator('#rodos-memory-preview').textContent(),/机器交互/);
 pass('Forgetting updates the original memory and removes its content from the generated preview');
 await page.screenshot({path:path.join(dir,'memory-settings.png'),fullPage:true});
 assert.match(await page.locator('#rodos-schedules').innerText(),/每小时/);
 assert.match(await page.locator('#rodos-schedules').innerText(),/周五/);
 await page.click('#rodos-refresh-feishu');await wait(()=>maintenanceRuns[0].id!=='maintenance_prior');
 await page.waitForSelector('#rodos-active-maintenance [data-key="maintenance:'+maintenanceRuns[0].id+'"]');
 const pendingText=await page.locator('#rodos-active-maintenance').innerText();assert.match(pendingText,/启动|等待|运行|处理/);assert.doesNotMatch(pendingText,/同步成功|全量.*已完成/);
 pass('Manual full refresh shows a real pending run instead of claiming completion');
 maintenanceRuns[0].status='succeeded';maintenanceRuns[0].completed_at=clock;maintenanceRuns[0].summary='本轮已完成登记来源同步。';
 await page.click('#rodos-settings-refresh');await page.waitForTimeout(100);
 await page.click('[data-page="agent"]');await page.click('#rodos-inspect-now');
 await wait(()=>maintenanceRuns[0].kind==='inspection'&&maintenanceRuns[0].id!=='maintenance_prior');
 const check=maintenanceRuns[0];check.status='succeeded';check.completed_at=clock;check.summary='已同步全部登记项目；一项来源需要关注。';
 check.inspection={observed_at:clock,project_ids:[project.id],issues:[{kind:'source',summary:'有一项来源需要重新授权。'}],projects:[],read_only:true};
 await page.click('#refresh-workspace');await page.waitForFunction(()=>document.querySelector('#rodos-latest-inspection').textContent.includes('一项'));
 assert.match(await page.locator('#rodos-inspection-history').textContent(),/上次已完成同步与只读检查/);
 pass('Weekly inspection brief and previous inspection history remain distinct from the refresh run');
 await page.click('[data-page="global"]');await page.waitForSelector('[data-peer-approve="node_workbuddy"]');
 assert.equal(await page.locator('[data-peer-bind], [data-peer-rotate]').count(),0);
 assert.match(await page.locator('[data-key="user:member_workbuddy"]').innerText(),/482731|确认节点归属/);
 await page.click('[data-peer-approve="node_workbuddy"][data-peer-request="enroll_fixture_workbuddy"]');
 await page.waitForSelector('#confirm-dialog[open]');
 assert.match(await page.locator('#confirm-message').innerText(),/482731/);
 assert.match(await page.locator('#confirm-message').innerText(),/私钥.*留在节点|不需要传递文件/);
 await page.click('#confirm-action');await wait(()=>bound);
 await page.waitForFunction(()=>document.querySelector('[data-key="user:member_workbuddy"]').textContent.includes('已确认，待节点核对'));
 assert.equal(downloads,0);assert.equal(await page.locator('[data-peer-approve="node_workbuddy"]').count(),0);
 assert.doesNotMatch(await page.locator('[data-key="user:member_workbuddy"]').innerText(),/互派已启用/);
 pass('The member card confirms the automatic enrollment code and request hash without downloading or carrying a private key');
 const hourly=page.locator('[data-rodos-schedule="hourly_refresh"]');
 await hourly.click();await wait(()=>schedules[0].enabled===false);
 await page.waitForFunction(()=>document.querySelector('[data-rodos-schedule="hourly_refresh"]').textContent==='启用');
 await hourly.click();await wait(()=>schedules[0].enabled===true&&schedules[0].revision===3);
 await page.waitForFunction(()=>document.querySelector('[data-rodos-schedule="hourly_refresh"]').textContent==='停用');
 pass('Maintenance schedules can be disabled and enabled again using the current saved revision');
 await page.screenshot({path:path.join(dir,'maintenance-members.png'),fullPage:true});
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.external_requests,[]);
 assert.equal(posted.filter(p=>p.path.endsWith('/approve-enrollment')).length,1);assert.equal(downloads,0);
 pass('No browser exceptions or external connections; all writes stayed in memory fixtures');
 report.request_counts=Object.fromEntries([...new Set(posted.map(p=>p.path))].map(p=>[p,posted.filter(item=>item.path===p).length]));
 report.download_count=downloads;
})().catch(error=>{report.failure=error.stack;console.error(error);process.exitCode=1;}).finally(async()=>{
 report.finished_at=new Date().toISOString();fs.writeFileSync(path.join(dir,'report.json'),JSON.stringify(report,null,2),{mode:0o600});
 if(browser)await browser.close();console.log('Evidence: '+dir);
});
