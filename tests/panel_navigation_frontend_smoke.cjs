/* Current Panel sources in Chromium; all API reads and writes are in-memory fixtures. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require('/Users/alexliu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'..');
const sources=Object.fromEntries(['central.html','central.js','central.css'].map(name=>[name,fs.readFileSync(path.join(root,'server/static',name),'utf8')]));
const dir=path.resolve(root,'../evidence/panel_navigation_ui','run_'+new Date().toISOString().replace(/[:.]/g,'-'));
fs.mkdirSync(dir,{recursive:true,mode:0o700});
const report={mode:'Current static HTML/JS/CSS in Chromium; isolated in-memory API fixtures; no live model, Feishu or node calls',
  started_at:new Date().toISOString(),source_sha256:Object.fromEntries(Object.entries(sources).map(([name,text])=>[name,crypto.createHash('sha256').update(text).digest('hex')])),checks:[],errors:[],external_requests:[],unknown_requests:[]};
const clone=v=>JSON.parse(JSON.stringify(v));
const at='2026-10-24T02:00:00Z';
const pass=name=>{report.checks.push({name,passed:true});console.log('PASS '+name);};
const until=async test=>{for(let n=0;n<160;n++){if(await test())return;await new Promise(resolve=>setTimeout(resolve,25));}throw Error('Fixture condition timed out');};
let browser,lastPage;

async function fixture(role='admin',viewport={width:1440,height:1080}){
  const context=await browser.newContext({viewport});
  const page=await context.newPage();lastPage=page;page.setDefaultTimeout(10000);
  page.on('pageerror',error=>report.errors.push({role,message:error.message}));
  page.on('dialog',dialog=>dialog.accept());
  const admin={id:'fixture_admin',username:'admin',display_name:'雨寒',roles:['admin'],enabled:true};
  const node={node_id:'node_workbuddy',public_id:'node_workbuddy',member_id:'fixture_researcher',display_name:'WorkBuddy',enabled:true};
  const researcher={id:'fixture_researcher',username:'workbuddy',display_name:'研发成员',roles:['researcher'],enabled:true,nodes:[node]};
  const user=role==='admin'?admin:researcher;
  const allProjects=[
    {id:'project_default',name:'RDOS 建设',description:'研发协作工作',is_default:true,status:'active',node_ids:[node.node_id],member_ids:[researcher.id],folder_url:'https://scn33386s7ui.feishu.cn/drive/folder/FixtureDefault'},
    {id:'project_beta',name:'百特 Shared Skill 打磨项目',description:'共享 Skill 资料整理',status:'active',node_ids:[node.node_id],member_ids:[researcher.id],folder_url:'https://scn33386s7ui.feishu.cn/drive/folder/FixtureBeta'},
    {id:'project_retired',name:'已停用项目',description:'历史记录',status:'inactive',node_ids:[],member_ids:[]}
  ];
  const projects=role==='admin'?allProjects:allProjects.filter(p=>p.id==='project_beta');
  const threads=[
    {id:'thread_default',project_id:'project_default',title:'当前工作',created_at:at},
    {id:'thread_beta',project_id:'project_beta',title:'共享 Skill 范围',created_at:at},
    {id:'thread_organization',project_id:'',title:'机构讨论',created_at:at}
  ];
  const task=(projectId,suffix,status='active')=>({id:projectId+'_'+suffix,task_id:projectId+'_'+suffix,project_id:projectId,
    title:(projectId==='project_default'?'RDOS':'Shared Skill')+' '+({active:'当前工作',closed:'已关闭工作',review:'待质量审阅',blocked:'需补充输入'}[suffix]||suffix),
    owner_id:researcher.id,executor_node_ids:[node.node_id],goal:'整理已经核实的工作资料。',deliverable:'提交可阅读的结果。',task_type:'deliverable',
    requirements_version:1,record_revision:1,business_status:status,output_zone:'project',dispatch_to:['agent'],return_to:['agent'],input_refs:[],
    completion_rule:{kind:suffix==='review'?'human_review':'receipt_recorded'},
    ...(suffix==='closed'?{close_reason:'completed',result_status:'completed',receipt_status:'verified'}:{}),
    ...(suffix==='blocked'?{result_status:'blocked',execution_result:{summary:'还缺一份项目输入。'}}:{})});
  const phase3=projectId=>({project:clone(allProjects.find(p=>p.id===projectId)),nodes:[clone(node)],last_refresh:{at,complete:true},
    scope:{zones:{project:{source_url:'https://scn33386s7ui.feishu.cn/drive/folder/'+(projectId==='project_default'?'FixtureDefault':'FixtureBeta')},agent:{source_url:'https://scn33386s7ui.feishu.cn/drive/folder/FixtureAgent'}},
      task_table_url:'https://scn33386s7ui.feishu.cn/base/FixtureBase?table=FixtureTasks',event_table_url:'https://scn33386s7ui.feishu.cn/base/FixtureBase?table=FixtureEvents'},
    tasks:[task(projectId,'active'),task(projectId,'closed','closed'),task(projectId,'review','pending_review'),task(projectId,'blocked')],
    drafts:role==='admin'?[{id:projectId+'_draft',task_id:projectId+'_draft_task',status:'draft',payload:{...task(projectId,'draft'),title:'待确认的资料整理'}}]:[],
    documents:[{id:projectId+'_document',title:projectId==='project_default'?'工作资料简报':'Shared Skill 版本说明',zone:'project',revision:3,source_url:'https://scn33386s7ui.feishu.cn/docx/'+(projectId==='project_default'?'FixtureReport':'FixtureSkillReport')}],
    events:[{id:projectId+'_progress',project_id:projectId,event_type:'progress',claimed_node_id:node.node_id,summary:projectId==='project_default'?'已整理三份工作资料。':'已整理一份 Skill 说明。',created_at:at,
      task_refs:[{task_id:projectId+'_active',requirements_version:1}],content:'INTERNAL_PROTOCOL_PAYLOAD_DO_NOT_PROJECT'},
      {id:projectId+'_review',project_id:projectId,event_type:'review',claimed_node_id:node.node_id,summary:'审阅意见已写回。',created_at:at,task_refs:[{task_id:projectId+'_review',requirements_version:1}]}],operations:[]});
  const rag={id:'rag_method',category:'selected_rag',name:'研发方法资料',path:'方法/研发方法资料.md',type:'file',content_status:'full_text',available:true,version:'sha256:ragversion',source_revision:'3',source_url:'https://scn33386s7ui.feishu.cn/docx/FixtureRag'};
  const skill={id:'skill_package',category:'shared_skills',name:'说人话',path:'说人话',type:'skill',content_status:'skill_package',available:true,version:'sha256:skillversion',file_count:2,source_url:'https://scn33386s7ui.feishu.cn/drive/folder/FixtureSkill'};
  const skillMain={id:'skill_main',name:'SKILL.md',relative_path:'SKILL.md',type:'file',content_status:'full_text',version:'sha256:skillmain',source_url:'https://scn33386s7ui.feishu.cn/file/FixtureMain'};
  const skillRef={id:'skill_reference',name:'examples.md',relative_path:'references/examples.md',type:'file',content_status:'full_text',version:'sha256:skillref',source_url:'https://scn33386s7ui.feishu.cn/file/FixtureReference'};
  const knowledge={sources:['selected_rag','shared_skills'].map(category=>({category,configured:true,last_success_at:at,status:'ready',source_url:'https://scn33386s7ui.feishu.cn/drive/folder/'+(category==='selected_rag'?'FixtureRagRoot':'FixtureSkillRoot')})),items:[rag,skill]};
  const posted=[],requests=[],messages=[],runs=[];
  const workspace=projectId=>({user:clone(user),users:[clone(admin),clone(researcher)],project:clone(allProjects.find(p=>p.id===projectId)),messages:clone(messages.filter(m=>m.project_id===projectId)),runs:clone(runs.filter(r=>r.project_id===projectId)),tasks:[],runtime:{paused:false}});
  const inspection={id:'fixture_inspection',kind:'inspection',status:'succeeded',project_ids:['project_default','project_beta'],created_at:at,completed_at:at,summary:'全部登记项目已完成同步与检查。',inspection:{observed_at:at,issues:[],projects:[{id:'project_default'},{id:'project_beta'}],read_only:true}};
  await page.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url()),p=url.pathname,method=req.method();
    const send=(body,status=200,type='application/json')=>route.fulfill({status,contentType:type,body:typeof body==='string'?body:JSON.stringify(body)});
    if(url.hostname!=='rdos.test'){report.external_requests.push({role,url:req.url()});return route.abort();}
    if(p==='/')return send(sources['central.html'],200,'text/html');
    if(/^\/(?:static\/)?central\.(?:css|js)$/.test(p))return send(sources[path.basename(p)],200,p.endsWith('.css')?'text/css':'text/javascript');
    if(p==='/favicon.ico')return send('',204,'text/plain');
    requests.push({method,path:p,query:url.search});
    if(p==='/api/lab/info')return send({env_id:'navigation_fixture',generation:'navigation_fixture_v1',project_id:'lab_project_phase0',runtime:{model_mode:'fixture'}});
    if(p==='/api/central/auth/me'||p==='/api/central/auth/login')return send({user:clone(user)});
    if(p==='/api/central/auth/logout'){posted.push({path:p,body:{}});return send({ok:true});}
    if(p==='/api/central/projects')return send({items:clone(projects),nodes:[clone(node)],default_folder_url:allProjects[0].folder_url});
    if(p==='/api/central/users')return send({items:[clone(admin),clone(researcher)]});
    if(p==='/api/central/threads'){
      if(method==='GET')return send({items:clone(threads.filter(t=>String(t.project_id||'')===String(url.searchParams.get('project_id')||'')))});
      const body=req.postDataJSON();posted.push({path:p,body});
      const item={id:'thread_created_'+posted.filter(v=>v.path===p).length,project_id:body.project_id||'',title:body.title,created_at:at};threads.unshift(item);return send({item:clone(item)});
    }
    if(p==='/api/central/workspace')return send(workspace(url.searchParams.get('project_id')));
    if(p==='/api/central/overview')return send({user:clone(user),users:[clone(admin),clone(researcher)],messages:[],runs:[],tasks:[],runtime:{paused:false}});
    if(p==='/api/central/phase3/workspace')return send(phase3(url.searchParams.get('project_id')));
    if(p==='/api/central/knowledge')return send(clone(knowledge));
    if(p==='/api/central/knowledge/items/skill_package')return send({item:clone(skill),version:skill.version,entrypoint_id:skillMain.id,files:[clone(skillMain),clone(skillRef)],content:'# 说人话\n保留事实，先把结果说清楚。'});
    if(p==='/api/central/knowledge/items/skill_reference')return send({item:clone(skillRef),version:skillRef.version,content:'# 示例\n把实际变化告诉读者。'});
    if(p==='/api/central/knowledge/items/rag_method')return send({item:clone(rag),version:rag.version,content:'# 研发方法资料\n先核实输入，再讨论交付。'});
    if(p==='/api/central/settings/agent')return send({version:4,content:'先说明工作结果、问题和必要决定。',updated_at:at,updated_by:admin.id,file_updated:true});
    if(p==='/api/central/settings/memories')return send({items:[{id:'memory_fixture',topic:'面向人的产出',content:'让人能读懂。',scope:'organization',applicability:{kind:'organization'},version:1,status:'active',evidence_kind:'explicit',sources:[{source_kind:'panel_settings'}]}],has_more:false});
    if(p==='/api/central/settings/memory-preview')return send({available:true,generated:true,content:'# 中央共享记忆\n让人能读懂。',summary_updated_at:at});
    if(p==='/api/central/maintenance/state')return send({paused:false,heartbeat_running:true,active_run:null,latest_inspection:clone(inspection),schedules:[{id:'hourly_refresh',enabled:true,revision:1,next_due_at:'2026-10-24T03:00:00Z'},{id:'weekly_inspection',enabled:true,revision:1,next_due_at:'2026-10-30T15:00:00Z'}]});
    if(p==='/api/central/maintenance/runs')return send({items:[clone(inspection)],total:1});
    if(p==='/api/central/peer/nodes')return send({nodes:[]});
    if(p==='/api/central/chat'){
      const body=req.postDataJSON();posted.push({path:p,body});
      const run={id:'fixture_chat_'+runs.length,kind:'chat',trigger:'manual',status:'running',steps:[],thread_id:body.thread_id,project_id:body.project_id||'',created_at:at,operation_id:body.operation_id};runs.push(run);
      messages.push({id:run.id+':user',role:'user',content:body.message,user_id:user.id,run_id:run.id,thread_id:run.thread_id,project_id:run.project_id,created_at:at,operation_id:body.operation_id});return send({run_id:run.id});
    }
    if(p.startsWith('/api/central/runs/'))return send({run:clone(runs.find(r=>r.id===p.split('/').at(-1))||{})});
    report.unknown_requests.push({role,method,path:p});return send({detail:{message:'测试 fixture 未登记此请求。'}},500);
  });
  await page.goto('http://rdos.test/');await page.waitForSelector('#app-view:not(.hidden)');
  return {page,context,posted,requests,projects,threads,messages,runs,phase3,user};
}

async function visible(page,selector){return page.locator(selector).isVisible();}
async function noHorizontalOverflow(page,name){
  const size=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,width:window.innerWidth}));
  assert(size.scroll<=size.width+1,name+': page overflows '+size.scroll+' > '+size.width);
}
async function checkOnlyGroup(page,attribute,wanted){
  const groups=await page.locator('['+attribute+']').evaluateAll((nodes,attr)=>nodes.map(node=>({name:node.getAttribute(attr),visible:!!(node.offsetWidth||node.offsetHeight||node.getClientRects().length)})),attribute);
  assert(groups.some(g=>g.name===wanted&&g.visible),'Expected visible group '+wanted);
  assert(groups.every(g=>g.name===wanted||!g.visible),'Unselected content remains visible: '+JSON.stringify(groups));
}

(async()=>{
  browser=await chromium.launch({channel:'chrome',headless:true,args:['--disable-background-networking','--no-first-run']});
  const f=await fixture(),{page}=f;
  await page.waitForSelector('[data-project-view-id="project_default"]');
  assert.equal((await page.locator('.brand').innerText()).trim(),'百特教育研发协同工作台 RDOS');
  assert.deepEqual(await page.locator('#main-nav .page-tab:visible').evaluateAll(nodes=>nodes.map(n=>n.dataset.page)),['projects','agent','collaboration','knowledge','global','account']);
  assert.deepEqual(await page.locator('#main-nav .page-tab:visible').allTextContents(),['项目管理','中央Agent','协作与任务','共享知识管理','设置','我的账号']);
  assert.match(await page.locator('#current-user').innerText(),/雨寒/);
  const header=await page.locator('#current-user').evaluate(el=>{const user=el.getBoundingClientRect(),nav=document.querySelector('#main-nav').getBoundingClientRect(),brand=document.querySelector('.brand').getBoundingClientRect();return {userLeft:user.left,navRight:nav.right,userRight:user.right,width:window.innerWidth,sameRow:Math.min(user.bottom,nav.bottom,brand.bottom)>Math.max(user.top,nav.top,brand.top)};});
  assert(header.sameRow,'Title, navigation and username are not on one desktop row');
  assert(header.userLeft>=header.navRight-1,'Username is not to the right of the navigation');
  assert(header.width-header.userRight<100,'Username is not at the right edge');
  const navAlignment=await page.locator('#main-nav').evaluate(el=>({navLeft:el.getBoundingClientRect().left,firstTabLeft:el.querySelector('.page-tab:not(.hidden)').getBoundingClientRect().left}));
  assert(navAlignment.firstTabLeft-navAlignment.navLeft>100,'Top tabs are not aligned toward the account area');
  assert(await page.locator('#main-nav').evaluate(el=>!!el.closest('.topbar')),'Main tabs are not in the top header');
  await noHorizontalOverflow(page,'desktop projects');
  pass('Header shows the requested workbench title, six top tabs and the current username on the far right');

  assert(await page.locator('#new-project').evaluate(el=>!!el.closest('.heading-actions')));
  assert.equal(await page.locator('#refresh-workspace').count(),0);
  assert(await page.locator('#project-navigation').evaluate(el=>!!el.closest('#workspace-sidebar')));
  assert.equal(await page.locator('[data-project-view-id="project_retired"]').count(),0);
  await page.click('[data-project-view-id="project_beta"]');
  assert.equal(await page.locator('#project-list .project-card').count(),1);
  assert.match(await page.locator('#project-list').innerText(),/百特 Shared Skill 打磨项目/);
  assert.doesNotMatch(await page.locator('#project-list').innerText(),/RDOS 建设/);
  await page.click('[data-project-view-select="manage"]');assert.equal(await page.locator('#project-list .project-card').count(),2);
  await page.check('#show-inactive-projects');await page.waitForSelector('[data-project-view-id="project_retired"]');
  await page.uncheck('#show-inactive-projects');assert.equal(await page.locator('[data-project-view-id="project_retired"]').count(),0);
  await page.click('#new-project');await page.waitForSelector('#project-dialog[open]');
  assert.equal((await page.locator('#project-dialog-title').innerText()).trim(),'创建项目');
  await page.click('#project-dialog [data-close="project-dialog"]');
  assert.equal(f.posted.length,0);pass('Project creation is beside the page title; project navigation and inactive visibility remain in the sidebar');
  await page.screenshot({path:path.join(dir,'desktop-projects.png'),fullPage:true});

  await page.click('[data-page="agent"]');await page.waitForSelector('#thread-select option[value="thread_default"]',{state:'attached'});
  for(const selector of ['#chat-project','#thread-select'])assert(await page.locator(selector).evaluate(el=>!!el.closest('#workspace-sidebar')),selector+' is outside the sidebar');
  assert(await page.locator('#new-thread').evaluate(el=>!!el.closest('.heading-actions')));
  assert.match(await page.locator('label[for="chat-project"]').innerText(),/项目范围/);
  assert.doesNotMatch(await page.locator('label[for="chat-project"]').innerText(),/讨论范围/);
  assert.equal(await visible(page,'#rodos-inspect-now'),false);
  await page.click('#new-thread');await page.waitForSelector('#thread-dialog[open]');
  await page.click('#thread-create');assert.equal(f.posted.filter(p=>p.path.endsWith('/threads')).length,0);
  await page.fill('#thread-title','临时主题');await page.click('#thread-dialog [data-close="thread-dialog"]');
  await page.waitForSelector('#confirm-dialog[open]');await page.click('#confirm-action');
  assert.equal(f.posted.filter(p=>p.path.endsWith('/threads')).length,0);
  await page.click('#new-thread');assert.equal(await page.locator('#thread-title').inputValue(),'');
  await page.fill('#thread-title','  共享 Skill 整理方式  ');await page.click('#thread-create');
  await until(()=>f.posted.some(p=>p.path.endsWith('/threads')));
  assert.deepEqual(f.posted.find(p=>p.path.endsWith('/threads')).body,{project_id:'project_default',title:'共享 Skill 整理方式'});
  await page.waitForSelector('#thread-dialog:not([open])',{state:'attached'});
  await until(async()=>await page.locator('#thread-select').inputValue()==='thread_created_1');
  assert.match(await page.locator('#thread-select option:checked').innerText(),/共享 Skill 整理方式/);
  pass('Central sidebar keeps scope and thread selection; the heading action requires a typed topic for new conversations');

  await page.fill('#chat-message','换项目时仍保留这段草稿。');
  await page.selectOption('#chat-project','project_beta');await until(async()=>await page.locator('#thread-select').inputValue()==='thread_beta');
  assert.equal(await page.locator('#chat-message').inputValue(),'');
  await page.selectOption('#chat-project','project_default');await until(async()=>await page.locator('#thread-select').inputValue()==='thread_created_1');
  assert.equal(await page.locator('#chat-message').inputValue(),'换项目时仍保留这段草稿。');
  await page.fill('#chat-message','第一行');await page.press('#chat-message','Shift+Enter');await page.type('#chat-message','第二行');
  assert.equal(await page.locator('#chat-message').inputValue(),'第一行\n第二行');
  assert.equal(f.posted.filter(p=>p.path.endsWith('/chat')).length,0);
  await page.locator('#chat-message').evaluate(el=>el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',isComposing:true,bubbles:true,cancelable:true})));
  assert.equal(f.posted.filter(p=>p.path.endsWith('/chat')).length,0);
  await page.press('#chat-message','Enter');await until(()=>f.runs.length===1);await page.waitForSelector('#messages .thinking');
  assert.match(await page.locator('#messages .user').innerText(),/第一行/);
  assert.equal((await page.locator('#messages .thinking .message-name').innerText()).trim(),'Rodos_中央Agent');
  assert.match(await page.locator('#messages .thinking').innerText(),/思考中/);
  const run=f.runs[0];run.status='succeeded';
  f.messages.push({id:run.id+':assistant',role:'assistant',content:'已核对当前项目的工作要求。',project_id:run.project_id,thread_id:run.thread_id,run_id:run.id,created_at:at});
  await page.waitForFunction(()=>document.querySelector('#messages').textContent.includes('已核对当前项目的工作要求'));
  pass('Project/thread switching preserves only the matching draft; Enter sends, Shift+Enter inserts a line and IME confirmation does not send');
  const chatLayout=await page.evaluate(()=>{const box=document.querySelector('#messages'),form=document.querySelector('#chat-form'),input=document.querySelector('#chat-message'),long=document.createElement('p');long.style.whiteSpace='pre-wrap';long.textContent=('这是一段较长的对话记录。\n').repeat(200);box.append(long);const result={messageOverflow:box.scrollHeight>box.clientHeight,formBottom:form.getBoundingClientRect().bottom,inputHeight:input.getBoundingClientRect().height,viewportHeight:innerHeight};long.remove();return result;});
  assert(chatLayout.messageOverflow,'Long conversations should scroll inside the message area');
  assert(chatLayout.formBottom<=chatLayout.viewportHeight-8,'Composer should remain in the visible lower part of the page');
  assert(chatLayout.inputHeight<=52,'An empty composer should occupy one line');
  pass('Long chats scroll above a one-line composer kept near the viewport bottom');
  await page.screenshot({path:path.join(dir,'desktop-central.png'),fullPage:true});

  await page.click('[data-page="collaboration"]');await page.waitForSelector('[data-key="phase3-task:project_default_active"]');
  assert.equal((await page.locator('#phase3-sync').innerText()).trim(),'同步飞书内容');
  assert(await page.locator('#phase3-project').evaluate(el=>!!el.closest('#workspace-sidebar')));
  for(const selector of ['#phase3-sync','#phase3-new'])assert(await page.locator(selector).evaluate(el=>!!el.closest('.heading-actions')));
  for(const selector of ['#phase3-summary','#phase3-sync-last'])assert(await page.locator(selector).evaluate(el=>!!el.closest('#workspace-sidebar')));
  assert.equal(await page.locator('#phase3-show-closed').isChecked(),false);
  assert.doesNotMatch(await page.locator('#phase3-tasks').innerText(),/已关闭工作/);
  assert.match(await page.locator('#phase3-tasks').innerText(),/RDOS 当前工作/);
  assert.doesNotMatch(await page.locator('#phase3-tasks').innerText(),/Shared Skill 当前工作/);
  await checkOnlyGroup(page,'data-collaboration-view','tasks');
  await page.check('#phase3-show-closed');assert.match(await page.locator('#phase3-tasks').innerText(),/RDOS 已关闭工作/);
  await page.uncheck('#phase3-show-closed');
  await page.click('[data-collaboration-view-select="events"]');await checkOnlyGroup(page,'data-collaboration-view','events');
  assert.match(await page.locator('#phase3-events').innerText(),/已整理三份工作资料/);
  assert.doesNotMatch(await page.locator('#phase3-events').innerText(),/INTERNAL_PROTOCOL_PAYLOAD_DO_NOT_PROJECT/);
  await page.click('[data-collaboration-view-select="review"]');await checkOnlyGroup(page,'data-collaboration-view','review');
  for(const selector of ['#phase3-drafts','#phase3-decisions','#phase3-documents'])assert(await visible(page,selector),selector+' not visible under review');
  assert.match(await page.locator('#phase3-drafts').innerText(),/待确认的资料整理/);
  assert.match(await page.locator('#phase3-decisions').innerText(),/待质量审阅|输入/);
  assert.match(await page.locator('#phase3-documents').innerText(),/工作资料简报/);
  pass('Collaboration partitions tasks, readable collaboration records and review items; closed tasks are hidden initially');
  await page.screenshot({path:path.join(dir,'desktop-review.png'),fullPage:true});
  await page.selectOption('#phase3-project','project_beta');await until(async()=>/Shared Skill 版本说明/.test(await page.locator('#phase3-documents').innerText()));
  assert.doesNotMatch(await page.locator('#phase3-documents').innerText(),/工作资料简报/);
  await page.click('[data-collaboration-view-select="tasks"]');
  assert.match(await page.locator('#phase3-tasks').innerText(),/Shared Skill 当前工作/);
  assert.doesNotMatch(await page.locator('#phase3-tasks').innerText(),/RDOS 当前工作|已关闭工作/);
  await page.check('#phase3-show-closed');assert.match(await page.locator('#phase3-tasks').innerText(),/Shared Skill 已关闭工作/);
  assert.doesNotMatch(await page.locator('#phase3-tasks').innerText(),/RDOS 已关闭工作/);
  const writes=f.posted.length;
  await page.click('[data-page="projects"]');await page.click('[data-page="collaboration"]');
  assert.equal(f.posted.length,writes);assert.equal(f.requests.filter(r=>r.path==='/api/central/phase3/sync').length,0);
  pass('Project changes keep tasks and outputs in scope; ordinary navigation does not synchronize or mutate business records');

  await page.click('[data-page="knowledge"]');await page.waitForSelector('[data-knowledge-open="rag_method"]');
  assert(await page.locator('#knowledge-categories').evaluate(el=>!!el.closest('#workspace-sidebar')));
  assert(await page.locator('#knowledge-list').evaluate(el=>!!el.closest('#workspace-sidebar')));
  assert.deepEqual(await page.locator('[data-knowledge-category] > span:first-child').allTextContents(),['Selected RAG','Shared Skills']);
  assert.equal((await page.locator('[data-knowledge-open="rag_method"]').innerText()).trim(),'研发方法资料');
  assert(await page.locator('#knowledge-heading-controls [data-knowledge-sync="selected_rag"]').isVisible());
  assert.equal((await page.locator('#knowledge-heading-controls [data-knowledge-sync="selected_rag"]').innerText()).trim(),'同步飞书内容');
  assert(await page.locator('#knowledge-heading-controls a').isVisible());
  await page.click('[data-knowledge-open="rag_method"]');await page.waitForFunction(()=>document.querySelector('#knowledge-content').textContent.includes('先核实输入'));
  await page.click('[data-knowledge-category="selected_rag"]');assert.equal(await visible(page,'#knowledge-list'),false);
  assert(await page.locator('[data-knowledge-category="shared_skills"]').isVisible());
  await page.click('[data-knowledge-category="shared_skills"]');await page.waitForSelector('[data-knowledge-open="skill_package"]');
  assert.equal((await page.locator('#knowledge-heading-controls [data-knowledge-sync="shared_skills"]').innerText()).trim(),'同步飞书内容');
  assert.equal(await page.locator('[data-knowledge-open="rag_method"]').count(),0);
  assert.equal((await page.locator('[data-knowledge-open="skill_package"]').innerText()).trim(),'说人话');
  await page.click('[data-knowledge-open="skill_package"]');await page.waitForFunction(()=>document.querySelector('#knowledge-content').textContent.includes('保留事实，先把结果说清楚'));
  assert.equal(await page.locator('#knowledge-list [data-knowledge-open]').count(),1);
  await page.locator('.knowledge-package-files > summary').click();
  await page.click('[data-knowledge-file="skill_reference"]');await page.waitForFunction(()=>document.querySelector('#knowledge-content').textContent.includes('把实际变化告诉读者'));
  assert.match(await page.locator('#knowledge-content').innerText(),/references\/examples.md/);
  pass('Knowledge sidebar shows names only, collapses long lists, and keeps sync/source actions beside the heading');
  await page.screenshot({path:path.join(dir,'desktop-knowledge.png'),fullPage:true});

  await page.click('[data-page="global"]');await page.waitForFunction(()=>document.querySelector('#rodos-policy-content').value.includes('先说明工作结果'));
  await checkOnlyGroup(page,'data-settings-view','memory');
  await page.fill('#rodos-policy-content','我的职责编辑尚未保存。');
  await page.click('#rodos-memory-tab');await page.click('#rodos-memory-add');await page.fill('#rodos-memory-topic','汇报约定');await page.fill('#rodos-memory-content','我的记忆编辑尚未保存。');
  await page.click('[data-settings-view-select="accounts"]');await checkOnlyGroup(page,'data-settings-view','accounts');
  assert(await visible(page,'#people-list'));assert(await visible(page,'#runtime-state'));
  assert(await page.locator('.member-card .member-actions').first().isVisible());
  assert(await page.locator('.member-card .member-node-actions').first().isVisible());
  assert.equal(await visible(page,'#rodos-inspect-now'),false);
  await page.click('[data-settings-view-select="sync"]');await checkOnlyGroup(page,'data-settings-view','sync');
  assert(await visible(page,'#rodos-inspect-now'));assert(await visible(page,'#rodos-schedules'));
  assert.match(await page.locator('[data-settings-view="sync"]').innerText(),/定时同步飞书信息/);
  assert.match(await page.locator('#rodos-latest-inspection').innerText(),/全部登记项目/);
  await page.click('[data-settings-view-select="memory"]');
  assert.equal(await page.locator('#rodos-memory-topic').inputValue(),'汇报约定');
  assert.equal(await page.locator('#rodos-memory-content').inputValue(),'我的记忆编辑尚未保存。');
  await page.click('#rodos-agent-tab');assert.equal(await page.locator('#rodos-policy-content').inputValue(),'我的职责编辑尚未保存。');
  assert(!f.posted.some(p=>/settings|maintenance/.test(p.path)),'Section selection wrote settings or launched maintenance');
  pass('Settings partition memory, members/services and synchronization; system inspection is only in synchronization and section changes preserve unsaved edits');
  await page.click('[data-settings-view-select="sync"]');await page.screenshot({path:path.join(dir,'desktop-sync.png'),fullPage:true});

  await page.click('[data-page="account"]');assert.equal((await page.locator('#page-title').innerText()).trim(),'我的账号');
  assert.match(await page.locator('#account-profile').innerText(),/雨寒|admin/);
  await page.click('#change-my-password');await page.waitForSelector('#password-dialog[open]');
  assert.equal((await page.locator('#password-dialog-title').innerText()).trim(),'修改我的密码');
  await page.click('#password-dialog [data-close="password-dialog"]');
  assert(!f.posted.some(p=>/password/.test(p.path)));
  pass('The sixth tab exposes the current account, existing password dialog and logout without changing credentials');

  await page.setViewportSize({width:390,height:844});
  for(const main of ['projects','agent','collaboration','knowledge','global','account']){
    await page.locator('#main-nav [data-page="'+main+'"]').click();
    await noHorizontalOverflow(page,'mobile '+main);
    assert(await page.locator('#main-nav [data-page="'+main+'"]').isVisible());
    if(main!=='account')assert(await page.locator('#workspace-sidebar').isVisible());
  }
  await page.click('[data-page="agent"]');await page.screenshot({path:path.join(dir,'mobile-central.png'),fullPage:true});
  pass('All six tabs and their sidebars remain usable at 390 px without page-level horizontal overflow');

  const research=await fixture('researcher');const rp=research.page;
  await rp.waitForSelector('[data-project-view-id="project_beta"]');
  assert.deepEqual(await rp.locator('#main-nav .page-tab:visible').evaluateAll(nodes=>nodes.map(n=>n.dataset.page)),['projects','collaboration','knowledge','account']);
  assert.equal(await rp.locator('[data-project-view-id="project_default"]').count(),0);
  assert.equal(await visible(rp,'#new-project'),false);
  await rp.click('[data-page="collaboration"]');await rp.waitForSelector('[data-key="phase3-task:project_beta_active"]');
  assert.equal(await visible(rp,'#phase3-sync'),false);assert.equal(await visible(rp,'#phase3-new'),false);
  await rp.click('[data-collaboration-view-select="review"]');assert.equal(await visible(rp,'#phase3-drafts'),false);assert.equal(await visible(rp,'#phase3-decisions'),false);
  await rp.click('[data-page="knowledge"]');await rp.waitForSelector('[data-knowledge-open="rag_method"]');
  assert.equal(await rp.locator('[data-knowledge-sync]:visible').count(),0);
  await rp.click('[data-page="account"]');assert.match(await rp.locator('#account-profile').innerText(),/研发成员|workbuddy/);
  const forbidden=research.requests.filter(r=>/^\/(users|threads|workspace|overview|settings|maintenance|peer)(?:\/|$)/.test(r.path.replace('/api/central','')));
  assert.deepEqual(forbidden,[],'Researcher navigation requested administrator resources');
  await rp.screenshot({path:path.join(dir,'researcher-account.png'),fullPage:true});
  pass('Researcher navigation preserves project scope and excludes central/admin screens, management buttons and administrator API reads');

  assert.deepEqual(report.errors,[]);assert.deepEqual(report.external_requests,[]);assert.deepEqual(report.unknown_requests,[]);
  assert.deepEqual([...new Set(f.posted.map(p=>p.path))].sort(),['/api/central/chat','/api/central/threads']);
  assert.equal(f.posted.filter(p=>p.path==='/api/central/threads').length,1);assert.equal(f.posted.filter(p=>p.path==='/api/central/chat').length,1);
  assert.equal(research.posted.length,0);
  report.fixture_writes=f.posted.map(p=>({path:p.path,project_id:p.body.project_id||'',thread_id:p.body.thread_id||''}));
  report.request_count=f.requests.length+research.requests.length;
  pass('No browser exceptions, unknown APIs or external connections; only the intended conversation and message were created in memory');
})().catch(async error=>{report.failure=error.stack;console.error(error);process.exitCode=1;if(lastPage)await lastPage.screenshot({path:path.join(dir,'failure.png'),fullPage:true}).catch(()=>{});}).finally(async()=>{
  report.finished_at=new Date().toISOString();fs.writeFileSync(path.join(dir,'report.json'),JSON.stringify(report,null,2),{mode:0o600});
  if(browser)await browser.close();console.log('Evidence: '+dir);
});
