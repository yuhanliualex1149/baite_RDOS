/* Browser behavior checks with isolated API fixtures. No live accounts or
 * Feishu resources are changed. */
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require('/Users/alexliu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'..');
const evidence=path.resolve(root,'../evidence/members_knowledge_ui');
const report={mode:'isolated browser fixtures',checks:[],errors:[],external_requests:[],started_at:new Date().toISOString()};
const pass=name=>{report.checks.push({name,passed:true});console.log('PASS '+name);};
let browser;
(async()=>{
  browser=await chromium.launch({channel:'chrome',headless:true,args:['--disable-background-networking','--no-first-run']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  page.setDefaultTimeout(8000);
  page.on('pageerror',error=>report.errors.push(error.message));
  await page.addInitScript(()=>{globalThis.copiedText='';Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{globalThis.copiedText=text;}}});});
  const admin={id:'admin',username:'admin',display_name:'管理员',roles:['admin'],enabled:true};
  const researcher={id:'member',username:'member',display_name:'研发同事',roles:['researcher'],enabled:true,nodes:[{node_id:'node_member',member_id:'member',display_name:'研发同事的 Local Agent'}]};
  const users=[admin,researcher];
  const nodes=researcher.nodes;
  const folder='https://scn33386s7ui.feishu.cn/drive/folder/DefaultFolder';
  const projects=[{id:'default',name:'RDOS建设',is_default:true,status:'active',node_ids:['node_member'],folder_url:folder,resolved_folder_url:folder,description:'当前项目'}, {id:'second',name:'本人成员项目',status:'active',node_ids:['node_member'],folder_url:folder,resolved_folder_url:folder}];
  const sources=[{category:'selected_rag',name:'Selected RAG',configured:true,status:'succeeded',source_url:'https://scn33386s7ui.feishu.cn/drive/folder/RagRoot',last_success_at:'2026-10-22T01:00:00Z',last_error:''},
    {category:'shared_skills',name:'Shared Skills',configured:true,status:'never',source_url:'https://scn33386s7ui.feishu.cn/drive/folder/SkillRoot',last_error:''}];
  const items=[
    {id:'rag1',category:'selected_rag',name:'方法说明.md',path:'方法说明.md',type:'file',content_status:'full_text',available:true,version:'v1',source_url:'https://scn33386s7ui.feishu.cn/file/RagDoc'},
    {id:'pdf1',category:'selected_rag',name:'调查报告.pdf',path:'参考/调查报告.pdf',type:'file',content_status:'attachment_only',available:true,version:'hash-pdf',source_url:'https://scn33386s7ui.feishu.cn/file/PdfDoc'},
    {id:'skill1',category:'shared_skills',name:'参考包草稿',path:'参考包草稿',type:'skill_package',content_status:'skill_package',available:true,version:'package:0.3',file_count:2,entrypoint_status:'ready',source_url:'https://scn33386s7ui.feishu.cn/drive/folder/SkillFolder'}
  ];
  const skillFiles=[{id:'skill-main',name:'SKILL.md',relative_path:'SKILL.md',relative_segments:['SKILL.md'],type:'file',content_status:'full_text',version:'0.3',source_url:'https://scn33386s7ui.feishu.cn/file/SkillDoc'},
    {id:'script1',name:'verify.py',relative_path:'scripts/verify.py',relative_segments:['scripts','verify.py'],type:'file',content_status:'file_listing',version:'hash-py',source_url:'https://scn33386s7ui.feishu.cn/file/ScriptDoc'}];
  const calls=[];let activeUser=admin,syncs=0,failSync=false;
  const catalog=()=>({sources,items});
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),pathname=url.pathname,method=request.method();
    const body=method==='GET'?null:request.postDataJSON();calls.push({pathname,method,body});
    const send=(value,status=200,type='application/json')=>route.fulfill({status,contentType:type,body:typeof value==='string'?value:JSON.stringify(value)});
    if(url.hostname!=='rdos.test'){report.external_requests.push(request.url());return route.abort();}
    if(pathname==='/')return send(fs.readFileSync(path.join(root,'server/static/central.html'),'utf8'),200,'text/html');
    if(pathname.endsWith('central.css'))return send(fs.readFileSync(path.join(root,'server/static/central.css'),'utf8'),200,'text/css');
    if(pathname.endsWith('central.js')){
      const source=fs.readFileSync(path.join(root,'server/static/central.js'),'utf8'),index=source.lastIndexOf('  (async()=>{try{');
      return send(source.slice(0,index)+'globalThis.h={state,phase3,knowledge,beginSession,refresh,render,loadPhase3,loadKnowledge,renderPhase3};})();',200,'text/javascript');
    }
    if(pathname==='/api/central/projects')return send({items:activeUser===admin?projects:projects.filter(p=>p.id==='second'),nodes,default_folder_url:folder});
    if(pathname==='/api/central/threads')return send({items:[{id:'thread1',title:'已有对话',created_at:'2026-10-22T01:00:00Z'}]});
    if(pathname==='/api/central/workspace')return send({user:activeUser,users,tasks:[],messages:[]});
    if(pathname==='/api/central/overview')return send({users,messages:[]});
    if(pathname==='/api/central/users'){
      if(method==='POST'){
        if(users.some(user=>user.username===body.username))return send({detail:{message:'账号已存在。'}},409);
        const user={...body,id:'new_member',enabled:true,nodes:[{node_id:'node_new',member_id:'new_member',display_name:'新同事的 Local Agent'}]};
        delete user.password;users.push(user);return send({user,password:'TemporaryPasswordOnly'});
      }
      return send({items:users});
    }
    if(pathname.startsWith('/api/central/users/')){
      const id=pathname.split('/').pop(),user=users.find(value=>value.id===id);
      Object.assign(user,body);return send({user,relogin_required:id===activeUser.id&&('roles'in body||'username'in body||body.enabled===false)});
    }
    if(pathname==='/api/central/phase3/workspace')return send({project:projects.find(p=>p.id===url.searchParams.get('project_id')),tasks:[{task_id:'t1',title:'当前工作',business_status:'active',requirements_version:1,completion_rule:{kind:'receipt_recorded'},executor_node_ids:['node_member']}],nodes,scope:{zones:{}},drafts:[],events:[],documents:[],read_only:activeUser!==admin});
    if(pathname==='/api/central/knowledge')return send(catalog());
    if(pathname==='/api/central/phase3/nodes/node_member/onboarding'&&method==='POST')return send({instruction:'请读取 http://rdos.test/nodes/node_member/start ，本节点：node_member。',task:{task_id:'setup-member'}});
    if(pathname.startsWith('/api/central/knowledge/items/')){
      const item=[...items,...skillFiles].find(value=>value.id===pathname.split('/').pop());
      if(item.id==='skill1')return send({item,version:item.version,content:'# 执行说明\n按当前要求开展工作。',files:skillFiles,entrypoint_id:'skill-main'});
      return send({item,version:item.version,content:item.content_status==='full_text'?'# 已确定资料\n原文事实。\n<script>globalThis.knowledgeInjected=true</script>':''});
    }
    if(pathname==='/api/central/knowledge/sync'){
      syncs++;if(failSync){sources[0].last_error='飞书读取暂时失败。';if(failSync==='business')return send({...catalog(),sync_result:{status:'failed'}});return send({detail:{message:'飞书读取暂时失败。'}},503);}
      sources[0].last_error='';sources[0].last_success_at='2026-10-22T02:00:00Z';return send({...catalog(),sync_result:{status:'succeeded'}});
    }
    return send({detail:{message:'fixture route not found'}},404);
  });
  await page.goto('http://rdos.test/');
  await page.evaluate(async user=>{h.beginSession(user);await h.refresh();},admin);
  assert.equal(await page.locator('#page-title').innerText(),'项目管理');
  assert.equal(await page.locator('[data-page="projects"]').first().innerText(),'项目管理');
  assert.doesNotMatch(await page.locator('body').innerText(),/保留项目工作区|使用本项目指定|可先起草任务|只续接本线程/);
  assert.equal(await page.locator('#phase3-nodes').count(),0);
  pass('Project management title, redundant copy removed, standalone node section absent');

  await page.click('[data-page="global"]');await page.click('[data-settings-view-select="accounts"]');
  assert(await page.locator('#add-user').isVisible());
  assert.equal(await page.locator('[data-key="user:admin"] [data-phase3-copy]').count(),0);
  assert.equal(await page.locator('[data-key="user:member"] [data-phase3-copy]').count(),1);
  await page.click('[data-phase3-copy="node_member"]');
  await page.waitForFunction(()=>globalThis.copiedText.includes('nodes/node_member/start'));
  assert.match(await page.evaluate(()=>globalThis.copiedText),/nodes\/node_member\/start/);
  assert.match(await page.evaluate(()=>globalThis.copiedText),/本节点：node_member/);
  pass('Onboarding prompt belongs to associated member card');

  await page.click('[data-edit-user="member"]');
  assert.equal(await page.locator('#user-username').isDisabled(),false);
  await page.fill('#user-username','updated-member');await page.fill('#user-display','更新称呼');
  await page.click('#user-form button[type="submit"]');await page.waitForSelector('#user-dialog',{state:'hidden'});
  assert.equal(users[1].username,'updated-member');
  await page.click('[data-user-enabled="member"]');assert.match(await page.locator('#confirm-message').innerText(),/节点任务保持原状/);
  await page.click('#confirm-action');await page.waitForFunction(()=>document.querySelector('[data-key="user:member"]').textContent.includes('已停用'));
  assert.equal(users[1].enabled,false);
  await page.click('[data-user-enabled="member"]');await page.click('#confirm-action');await page.waitForFunction(()=>document.querySelector('[data-user-enabled="member"]').textContent==='停用账号');
  assert.equal(users[1].enabled,true);
  pass('Username/name editing, login-only deactivate and re-enable');

  await page.click('#add-user');await page.fill('#user-username','new-member');await page.fill('#user-display','新同事');
  await page.click('#user-form button[type="submit"]');await page.waitForSelector('#credential-dialog[open]');
  assert.equal(await page.locator('#credential-password').inputValue(),'TemporaryPasswordOnly');
  assert.equal(await page.locator('[data-key="user:new_member"] [data-phase3-copy]').count(),1);
  assert.equal(await page.locator('#user-password').inputValue(),'');
  assert.doesNotMatch(await page.evaluate(()=>JSON.stringify({...localStorage,...sessionStorage})),/TemporaryPasswordOnly/);
  await page.click('#credential-done');await page.waitForFunction(()=>document.querySelector('#credential-password').value==='');
  pass('New researcher account prompt and one-time password display without browser storage');

  await page.click('[data-page="knowledge"]');await page.waitForFunction(()=>!!h.knowledge.data);
  assert.equal(await page.locator('[data-knowledge-category="context"]').count(),0);
  assert.equal(await page.locator('[data-knowledge-category]').count(),2);
  assert.match(await page.locator('#knowledge-source').innerText(),/Selected RAG/);
  if(await page.locator('[data-knowledge-category="selected_rag"]').getAttribute('aria-expanded')!=='true')await page.click('[data-knowledge-category="selected_rag"]');await page.click('[data-knowledge-open="rag1"]');
  await page.waitForFunction(()=>!!h.knowledge.detail);
  assert.match(await page.locator('#knowledge-content').innerText(),/原文事实/);
  assert.equal(await page.evaluate(()=>globalThis.knowledgeInjected),undefined);
  assert.equal(await page.locator('#knowledge-content script').count(),0);
  assert.equal(await page.locator('#knowledge-content a').getAttribute('href'),items[0].source_url);
  await page.click('[data-knowledge-open="pdf1"]');await page.waitForFunction(()=>h.knowledge.detail?.item.id==='pdf1');
  assert.match(await page.locator('#knowledge-content').innerText(),/正文尚未解析/);
  const before=syncs;await page.evaluate(()=>h.refresh());await page.waitForTimeout(100);assert.equal(syncs,before);
  await page.click('[data-knowledge-sync="selected_rag"]');await page.waitForFunction(()=>!h.knowledge.syncing);assert.equal(syncs,1);
  failSync=true;await page.click('[data-knowledge-sync="selected_rag"]');await page.waitForFunction(()=>!h.knowledge.syncing);
  assert.match(await page.locator('#knowledge-source').innerText(),/飞书读取暂时失败/);
  assert.equal(await page.locator('[data-knowledge-open="rag1"]').count(),1);
  failSync='business';await page.click('[data-knowledge-sync="selected_rag"]');await page.waitForFunction(()=>!h.knowledge.syncing);assert.match(await page.locator('#toast').innerText(),/部分资料未同步/);
  await page.click('[data-knowledge-category="shared_skills"]');assert.match(await page.locator('#knowledge-list').innerText(),/参考包草稿/);
  await page.click('[data-knowledge-open="skill1"]');await page.waitForFunction(()=>h.knowledge.detail?.item.id==='skill1');await page.locator('.knowledge-package-files>summary').click();
  await page.click('[data-knowledge-file="script1"]');await page.waitForFunction(()=>h.knowledge.fileDetail?.item.id==='script1');
  assert.match(await page.locator('#knowledge-content').innerText(),/未执行/);
  pass('Knowledge categories, safe full text, attachment limits, cache refresh and failed sync preservation');

  activeUser=researcher;const beforeMemberCalls=calls.length;
  await page.evaluate(()=>{for(const id of ['active-run','phase3-history','proposal-review','phase3-action-preview','password-target','confirm-message'])document.getElementById(id).textContent='PRIVATE_PREVIOUS_ADMIN_DATA';document.getElementById('user-display').disabled=true;document.getElementById('password-current').disabled=true;});
  await page.evaluate(async user=>{h.beginSession(user);await h.refresh();},researcher);
  assert.equal(await page.locator('.page-tab[data-page="agent"]').isVisible(),false);
  assert.equal(await page.evaluate(()=>document.body.textContent.includes('PRIVATE_PREVIOUS_ADMIN_DATA')),false);assert.equal(await page.locator('#user-display').isDisabled(),false);assert.equal(await page.locator('#password-current').isDisabled(),false);
  assert.equal(await page.locator('#people-list').textContent(),'');
  assert.equal(await page.locator('.page-tab[data-page="global"]').isVisible(),false);
  assert.equal(await page.locator('#new-project').isVisible(),false);
  assert.equal(await page.locator('.project-card').count(),1);
  await page.click('[data-project-work="second"]');await page.waitForFunction(()=>!!h.phase3.data);
  assert.match(await page.locator('#phase3-tasks').innerText(),/当前工作/);
  assert.equal(await page.locator('#phase3-new').isVisible(),false);
  assert.equal(await page.locator('#phase3-sync').isVisible(),false);
  assert.equal(await page.locator('[data-phase3-edit]').count(),0);
  assert.equal(await page.locator('[data-phase3-action]').count(),0);
  assert.equal(calls.slice(beforeMemberCalls).filter(call=>/\/(?:overview|threads|users|workspace)$/.test(call.pathname)&&!call.pathname.includes('/phase3/')).length,0);
  await page.click('[data-page="knowledge"]');await page.waitForFunction(()=>!!h.knowledge.data);
  await page.click('[data-knowledge-category="selected_rag"]');assert.equal(await page.locator('[data-knowledge-sync]').count(),0);
  pass('Researcher sees only assigned projects and read-only task/knowledge controls');

  activeUser=admin;
  await page.evaluate(async user=>{h.beginSession(user);await h.refresh();},admin);
  await page.click('[data-page="global"]');await page.click('[data-settings-view-select="accounts"]');await page.click('[data-edit-user="admin"]');await page.fill('#user-username','renamed-admin');
  await page.click('#user-form button[type="submit"]');await page.waitForSelector('#login-view',{state:'visible'});
  assert.match(await page.locator('#login-error').innerText(),/重新登录/);
  pass('Editing own login identity returns to login');
  assert.deepEqual(report.errors,[]);assert.deepEqual(report.external_requests,[]);
  report.finished_at=new Date().toISOString();fs.mkdirSync(evidence,{recursive:true});fs.writeFileSync(path.join(evidence,'report.json'),JSON.stringify(report,null,2));
  console.log('PASS '+report.checks.length+' browser checks');await browser.close();
})().catch(async error=>{report.failure=String(error.stack||error);fs.mkdirSync(evidence,{recursive:true});fs.writeFileSync(path.join(evidence,'report.json'),JSON.stringify(report,null,2));console.error(error);if(browser)await browser.close();process.exitCode=1;});
