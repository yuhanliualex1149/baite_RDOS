/* Isolated browser behavior checks. API responses are in-memory fixtures;
 * no requests reach the running Panel, Feishu, or a Local Agent. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('/Users/alexliu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const code = path.resolve(__dirname, '..');
const evidence = path.resolve(code, '../evidence/panel_management_ui');
const report = {mode:'isolated Chrome browser with in-memory API fixtures', started_at:new Date().toISOString(), checks:[], page_errors:[], external_requests:[], actual_runtime_modified:false};
const record = name => { report.checks.push({name,passed:true}); console.log('PASS '+name); };
let browser;

(async () => {
  browser = await chromium.launch({channel:'chrome',headless:true,args:['--disable-background-networking','--disable-component-update','--no-first-run']});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  page.setDefaultTimeout(8000);
  page.on('pageerror', error => report.page_errors.push(error.message));
  const user = {id:'admin',display_name:'管理员',username:'admin',roles:['admin']};
  const other = {id:'member',display_name:'同事',username:'member',roles:['researcher']};
  const folder = 'https://scn33386s7ui.feishu.cn/drive/folder/DefaultFolder';
  const nodes = [{node_id:'node_codex',public_id:'node_codex',display_name:'Codex',member_id:'admin'}, {node_id:'node_workbuddy',public_id:'node_workbuddy',display_name:'WorkBuddy',member_id:'member'}];
  const projects = [
    {id:'default',name:'RDOS建设',is_default:true,status:'active',revision:1,node_ids:nodes.map(node=>node.node_id),folder_url:folder,resolved_folder_url:folder,folder_source:'explicit'},
    {id:'second',name:'第二项目',status:'active',revision:1,node_ids:['node_workbuddy'],folder_url:'',resolved_folder_url:folder,folder_source:'default'},
  ];
  const calls = []; let synced = 0, syncFailure = false, syncIssues = 0, delayDefault = false;
  await page.route('**/*', async route => {
    const request=route.request(), url=new URL(request.url()), pathname=url.pathname;
    calls.push({path:pathname,query:url.search,method:request.method(),body:request.postDataJSON()});
    const send=(body,type='application/json',status=200)=>route.fulfill({status,contentType:type,body:typeof body==='string'?body:JSON.stringify(body)});
    if(url.hostname!=='rdos.test'){report.external_requests.push(request.url());return route.abort();}
    if(pathname==='/')return send(fs.readFileSync(path.join(code,'server/static/central.html'),'utf8'),'text/html');
    if(pathname.endsWith('central.js')){
      const source=fs.readFileSync(path.join(code,'server/static/central.js'),'utf8');
      const index=source.lastIndexOf('  (async()=>{try{');assert(index>0);
      return send(source.slice(0,index)+'globalThis.h={state,phase3,beginSession,refresh,render,renderPhase3,showPhase3Task};})();','text/javascript');
    }
    if(pathname.endsWith('central.css'))return send(fs.readFileSync(path.join(code,'server/static/central.css'),'utf8'),'text/css');
    if(pathname==='/api/central/projects'){
      if(request.method()==='POST'){
        const body=request.postDataJSON();projects.push({...body,id:'new',revision:1,status:'active',resolved_folder_url:body.folder_url||folder,folder_source:body.folder_url?'explicit':'default'});
        return send({item:projects.at(-1)});
      }
      return send({items:projects.filter(project=>url.searchParams.get('include_inactive')==='true'||project.status!=='inactive'),nodes,default_folder_url:folder});
    }
    if(pathname.endsWith('/threads'))return send({items:[]});
    if(pathname.endsWith('/overview'))return send({users:[user,other]});
    if(pathname==='/api/central/users'&&request.method()==='GET')return send({items:[user,other]});
    if(pathname.endsWith('/workspace')&&!pathname.includes('/phase3/'))return send({user,tasks:[],users:[user,other]});
    if(pathname.endsWith('/phase3/workspace')){
      const project=projects.find(project=>project.id===url.searchParams.get('project_id'));
      if(project?.id==='default'&&delayDefault)await new Promise(resolve=>setTimeout(resolve,400));
      return send({project,tasks:[],drafts:[],documents:[{id:'shared_rule',title:'RDOS 执行规范',zone:'agent',project_id:'default',revision:3}],events:[],nodes,scope:{zones:{}},last_refresh:synced?{at:'2026-10-22T01:00:00Z',complete:!syncFailure,error:syncFailure?'资料读取暂时失败。':'',issue_count:syncIssues,issue_summary:syncIssues?syncIssues+' 项记录待核对；有效进展已同步。':''}:null});
    }
    if(pathname.endsWith('/phase3/sync')){
      if(syncFailure)return send({detail:{message:'资料读取暂时失败。',code:'source_unavailable'}},'application/json',503);
      synced++;return send({complete:true});
    }
    if(pathname.endsWith('/phase3/drafts'))return send({item:{id:'draft',status:'draft'}});
    if(pathname.endsWith('/deactivation-preview'))return send({project_id:'second',revision:projects[1].revision,status:projects[1].status,
      ...(projects[1].deactivation?{resume_operation:{operation_id:projects[1].deactivation.operation_id,expected_revision:projects[1].deactivation.expected_revision}}:{}),
      tasks:[{task_id:'t',title:'需暂停的工作',business_status:'active'}]});
    if(pathname.endsWith('/deactivate')){
      const body=request.postDataJSON(),project=projects[1];
      if(!project.deactivation){project.status='deactivating';project.revision=2;project.deactivation={operation_id:body.operation_id,expected_revision:1,status:'partial',paused_task_ids:[],pending_task_ids:['t'],error:'读取暂时未完成'};}
      else{
        if(body.operation_id!==project.deactivation.operation_id||body.expected_revision!==1)return send({detail:{code:'operation_conflict',message:'继续停用必须保留原操作与原预期修订。'}},'application/json',409);
        project.status='inactive';project.revision=3;project.deactivation.status='succeeded';project.deactivation.paused_task_ids=['t'];project.deactivation.pending_task_ids=[];
      }
      return send({item:project});
    }
    if(pathname.endsWith('/reactivate')){projects[1].status='active';projects[1].revision+=1;return send({item:projects[1]});}
    return send({ok:true});
  });
  await page.goto('http://rdos.test/');
  await page.evaluate(async user=>{h.beginSession(user);await h.refresh();},user);
  assert.equal(await page.locator('#project-list .project-card').first().locator('h2').innerText(),'RDOS建设');
  assert.equal(await page.locator('[data-deactivate-project="default"]').count(),0);
  assert(calls.some(call=>call.path==='/api/central/projects'&&call.method==='GET'&&call.query.includes('include_inactive=true')));
  assert(await page.locator('#new-project').isVisible());
  assert(await page.locator('[data-edit-project="default"]').isVisible());
  record('Default pinned and protected; project controls visible');

  await page.click('[data-project-work="second"]');
  await page.waitForFunction(()=>document.querySelector('#phase3-summary').textContent.includes('第二项目'));
  assert.equal(await page.locator('#chat-project').inputValue(),'second');
  assert.doesNotMatch(await page.locator('#phase3-documents').innerText(),/RDOS 执行规范/);
  assert.match(await page.locator('#phase3-documents').innerText(),/尚无项目成果/);
  await page.evaluate(()=>{
    h.phase3.data.documents.push({id:'agent_output',title:'明确提交的成果',zone:'agent',revision:1});
    h.phase3.data.tasks=[{task_id:'reference',title:'已提交任务',business_status:'closed',result_refs:[{document_id:'agent_output',revision:1}],completion_rule:{kind:'receipt_recorded'}}];
    h.renderPhase3();
  });
  assert.match(await page.locator('#phase3-documents').innerText(),/明确提交的成果/);
  assert.doesNotMatch(await page.locator('#phase3-documents').innerText(),/RDOS 执行规范/);
  await page.evaluate(()=>{h.phase3.data.tasks=[];h.phase3.data.documents=h.phase3.data.documents.filter(document=>document.id!=='agent_output');h.renderPhase3();});
  record('Shared execution rules are not project artifacts; explicit Agent outputs remain visible');
  const before=synced;await page.click('#refresh-workspace');await page.waitForTimeout(100);assert.equal(synced,before);
  record('Shared project selection and cache-only refresh');

  await page.click('#phase3-new');
  assert.equal(await page.locator('#phase3-task-node option').count(),1);
  assert.equal(await page.locator('#phase3-task-node').inputValue(),'node_workbuddy');
  await page.fill('#phase3-task-title','仅供隔离浏览器检查');await page.fill('#phase3-task-goal','检查项目范围');await page.fill('#phase3-task-deliverable','操作结果');
  await page.click('#phase3-task-save');await page.waitForSelector('#phase3-task-dialog',{state:'hidden'});
  const draftCall=calls.find(call=>call.path==='/api/central/phase3/drafts');assert.equal(draftCall.query,'?project_id=second');assert.equal(draftCall.body.payload.project_id,undefined);
  await page.click('#phase3-sync');await page.waitForFunction(()=>document.querySelector('#phase3-sync-status').textContent.includes('已同步'));
  assert.equal(synced,1);assert(calls.some(call=>call.path==='/api/central/phase3/sync'&&call.query==='?project_id=second'));
  record('Only assigned nodes selectable; draft and sync use project query');

  syncFailure=true;await page.click('#phase3-sync');await page.waitForFunction(()=>document.querySelector('#phase3-sync-status').textContent.includes('同步未完成'));
  await page.click('#refresh-workspace');await page.waitForFunction(()=>document.querySelector('#phase3-sync-last').textContent.includes('资料读取暂时失败'));
  assert.match(await page.locator('#phase3-sync-last').innerText(),/上次成功同步/);assert.match(await page.locator('#phase3-summary').innerText(),/第二项目/);syncFailure=false;
  record('Failed sync remains visible while prior success and records survive');

  syncIssues=2;await page.click('#phase3-sync');await page.waitForFunction(()=>document.querySelector('#phase3-sync-status').textContent.includes('2 项记录待核对'));
  assert.match(await page.locator('#phase3-sync-last').innerText(),/2 项记录待核对/);assert(await page.locator('#phase3-sync-last').evaluate(node=>node.classList.contains('sync-warning')));syncIssues=0;
  record('Isolated bad records surface a warning instead of unconditional success');

  delayDefault=true;await page.selectOption('#phase3-project','default');await page.waitForTimeout(75);await page.selectOption('#phase3-project','second');await page.waitForTimeout(650);
  assert.match(await page.locator('#phase3-summary').innerText(),/第二项目/);assert.equal(await page.locator('#chat-project').inputValue(),'second');delayDefault=false;
  record('Late prior-project read does not replace current project');

  await page.click('[data-page="global"]');assert.equal(await page.locator('#phase3-nodes').count(),0);assert.equal(await page.locator('#people-list [data-phase3-copy]').count(),2);
  await page.click('[data-reset-password="member"]');await page.fill('#password-current','fixture_current_123');await page.fill('#password-new','fixture_new_123');await page.fill('#password-confirm','fixture_new_123');await page.click('#password-submit');await page.waitForSelector('#password-dialog',{state:'hidden'});
  assert(calls.some(call=>call.path==='/api/central/users/member/reset-password'));
  record('Node onboarding is on member cards in Settings; administrator reset is available');

  await page.click('[data-page="projects"]');await page.click('#new-project');await page.fill('#project-form-name','新项目');await page.check('#project-members input[value="node_workbuddy"]');assert.match(await page.locator('#project-folder-hint').innerText(),/Default/);await page.click('#save-project');await page.waitForSelector('[data-project-work="new"]');assert.equal(projects.at(-1).folder_url,'');
  record('Project form accepts omitted folder and shows Default fallback');

  await page.click('[data-deactivate-project="second"]');await page.waitForSelector('#confirm-dialog[open]');assert.match(await page.locator('#confirm-message').innerText(),/需暂停的工作/);await page.click('#confirm-action');await page.waitForSelector('#confirm-dialog',{state:'hidden'});
  assert.equal(projects[1].status,'deactivating');assert.equal(projects[1].revision,2);
  await page.evaluate(()=>{h.state.projectActionOps={};sessionStorage.clear();});
  await page.click('[data-deactivate-project="second"]');await page.waitForSelector('#confirm-dialog[open]');assert.match(await page.locator('#confirm-title').innerText(),/继续停用/);await page.click('#confirm-action');await page.waitForSelector('#confirm-dialog',{state:'hidden'});
  const attempts=calls.filter(call=>call.path==='/api/central/projects/second/deactivate');assert.equal(attempts.length,2);assert.deepEqual(attempts[1].body,attempts[0].body);assert.equal(attempts[1].body.expected_revision,1);
  record('Partial deactivation resumes original operation and revision after local recovery is cleared');
  await page.check('#show-inactive-projects');await page.waitForSelector('[data-reactivate-project="second"]');await page.click('[data-project-work="second"]');await page.waitForFunction(()=>document.querySelector('#phase3-new').disabled);
  assert(await page.locator('#phase3-new').isDisabled());await page.evaluate(()=>h.showPhase3Task());assert.equal(await page.locator('#phase3-task-dialog[open]').count(),0);
  await page.click('[data-page="projects"]');await page.click('[data-reactivate-project="second"]');await page.waitForSelector('#confirm-dialog[open]');await page.click('#confirm-action');await page.waitForSelector('#confirm-dialog',{state:'hidden'});
  record('Pause preview, inactive filter, task blocking and reactivation');

  await page.screenshot({path:(fs.mkdirSync(evidence,{recursive:true}),path.join(evidence,'fixture-projects.png')),fullPage:true});
  await page.click('#account-menu summary');await page.click('#change-my-password');await page.fill('#password-current','fixture_current_123');await page.fill('#password-new','fixture_new_123');await page.fill('#password-confirm','fixture_new_123');await page.click('#password-submit');await page.waitForSelector('#login-view:not(.hidden)');assert.equal(await page.locator('#password-new').inputValue(),'');
  assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.external_requests,[]);
  record('Self password change requires relogin and clears password fields');
  report.passed=true;
})().catch(error=>{report.passed=false;report.failure=error.message;console.error(error.stack);process.exitCode=1;})
  .finally(async()=>{if(browser)await browser.close();report.finished_at=new Date().toISOString();fs.mkdirSync(evidence,{recursive:true});fs.writeFileSync(path.join(evidence,'browser-fixture-report.json'),JSON.stringify(report,null,2));});
