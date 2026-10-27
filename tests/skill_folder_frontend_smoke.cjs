/* Real Chrome with isolated API fixtures: one root folder is one Skill. */
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('/Users/alexliu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const root=path.resolve(__dirname,'..'),out=path.resolve(root,'../evidence/skill_folder_ui');
const report={mode:'isolated Chrome API fixtures',checks:[],errors:[],started_at:new Date().toISOString()};
const pass=name=>{report.checks.push({name,passed:true});console.log('PASS '+name);};
let browser;
(async()=>{
 browser=await chromium.launch({channel:'chrome',headless:true,args:['--disable-background-networking']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(8000);
 page.on('pageerror',error=>report.errors.push(error.message));
 const admin={id:'admin',username:'admin',display_name:'管理员',roles:['admin'],enabled:true};
 const reader={id:'reader',username:'reader',display_name:'研发成员',roles:['researcher'],enabled:true};
 const sources=[{category:'selected_rag',name:'Selected RAG',configured:true,status:'succeeded',source_url:'https://scn33386s7ui.feishu.cn/drive/folder/RagRoot',last_success_at:'2026-10-22T10:00:00Z'},
  {category:'shared_skills',name:'Shared Skills',configured:true,status:'succeeded',source_url:'https://scn33386s7ui.feishu.cn/drive/folder/SkillRoot',last_success_at:'2026-10-22T10:00:00Z',skill_count:2}];
 const rag={id:'rag',name:'机构资料.md',path:'机构资料.md',category:'selected_rag',type:'file',content_status:'full_text',available:true,version:'rag-v1',source_url:'https://scn33386s7ui.feishu.cn/file/RagFile'};
 const alpha={id:'skill_alpha',category:'shared_skills',name:'内容制作_v2',path:'内容制作_v2',type:'skill_package',content_status:'skill_package',available:true,version:'package:alpha-v1',file_count:3,text_count:2,entrypoint_status:'ready',sync_status:'succeeded',source_url:'https://scn33386s7ui.feishu.cn/drive/folder/SkillAlpha'};
 const beta={id:'skill_beta',category:'shared_skills',name:'Skill Maintaining Log',path:'Skill Maintaining Log',type:'skill_package',content_status:'skill_package',available:true,version:'package:beta-v1',file_count:1,text_count:0,entrypoint_status:'missing',sync_status:'succeeded',source_url:'https://scn33386s7ui.feishu.cn/drive/folder/SkillBeta'};
 const files=[{id:'main',name:'SKILL.md',relative_path:'SKILL.md',relative_segments:['SKILL.md'],type:'file',content_status:'full_text',version:'main-v1',source_url:'https://scn33386s7ui.feishu.cn/file/AlphaMain'},
  {id:'reference',name:'事实 / 判断.md',relative_path:'references/事实 ／ 判断.md',relative_segments:['references','事实 / 判断.md'],type:'file',content_status:'full_text',version:'ref-v1',source_url:'https://scn33386s7ui.feishu.cn/file/AlphaRef'},
  {id:'script',name:'check.py',relative_path:'scripts/check.py',relative_segments:['scripts','check.py'],type:'file',content_status:'file_listing',version:'script-v1',source_url:'https://scn33386s7ui.feishu.cn/file/AlphaScript'}];
 const betaFiles=[{id:'archive',name:'log.zip',relative_path:'log.zip',relative_segments:['log.zip'],type:'file',content_status:'attachment_only',version:'zip-v1',source_url:'https://scn33386s7ui.feishu.cn/file/LogZip'}];
 const items=[rag,alpha,beta],calls=[];let failSync=false,delayReference=false,releaseReference,activeUser=admin;
 const catalog=()=>({sources,items});
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),pathname=url.pathname,method=request.method(),body=method==='GET'?null:request.postDataJSON();
  calls.push({pathname,method,body,version:url.searchParams.get('version'),skill_id:url.searchParams.get('skill_id'),package_version:url.searchParams.get('package_version')});
  const send=(value,status=200,type='application/json')=>route.fulfill({status,contentType:type,body:typeof value==='string'?value:JSON.stringify(value)});
  if(url.hostname!=='rdos.test')throw new Error('unexpected external request');
  if(pathname==='/')return send(fs.readFileSync(path.join(root,'server/static/central.html'),'utf8'),200,'text/html');
  if(pathname.endsWith('central.css'))return send(fs.readFileSync(path.join(root,'server/static/central.css'),'utf8'),200,'text/css');
  if(pathname.endsWith('central.js')){const code=fs.readFileSync(path.join(root,'server/static/central.js'),'utf8'),i=code.lastIndexOf('  (async()=>{try{');return send(code.slice(0,i)+'globalThis.h={state,knowledge,beginSession,refresh,loadKnowledge,openKnowledgeFile,renderKnowledge};})();',200,'text/javascript');}
  if(pathname==='/api/central/projects')return send({items:[],nodes:[]});
  if(pathname==='/api/central/users')return send({items:[admin,reader]});
  if(pathname==='/api/central/overview')return send({users:[admin,reader]});
  if(pathname==='/api/central/threads')return send({items:[]});
  if(pathname==='/api/central/knowledge')return send(catalog());
  if(pathname.startsWith('/api/central/knowledge/items/')){
   const id=pathname.split('/').pop(),version=url.searchParams.get('version');
   if(id===alpha.id)return send({item:alpha,version:alpha.version,files,entrypoint_id:'main',content:'# 内容制作规范\n本包主说明。\n<script>globalThis.packageInjected=true</script>'});
   if(id===beta.id)return send({item:beta,version:beta.version,files:betaFiles,entrypoint_id:null,content:''});
   if(id===rag.id)return send({item:rag,version:rag.version,content:'# 机构资料\nRAG仍按原资料展示。'});
   const file=[...files,...betaFiles].find(item=>item.id===id);if(!file)return send({detail:{message:'unknown file'}},404);
   if(id==='reference'&&delayReference)await new Promise(resolve=>releaseReference=resolve);
   return send({item:{...file,version},version,content:file.content_status==='full_text'?id==='reference'?'# 包内依据\nFROZEN_REFERENCE_VERSION_1':'# 主说明正文':''});
  }
  if(pathname==='/api/central/knowledge/sync'){
   assert.equal(body.category,'shared_skills');assert(body.operation_id);
   if(body.skill_id){assert.equal(body.skill_id,alpha.id);if(failSync){alpha.sync_status='failed';alpha.last_error='包内文件读取失败，保留原版。';return send({...catalog(),sync_result:{status:'failed',units:[{skill_id:alpha.id,status:'failed'}]}});}alpha.sync_status='succeeded';alpha.last_error='';alpha.version='package:alpha-v2';return send({...catalog(),sync_result:{status:'succeeded',units:[{skill_id:alpha.id,status:'succeeded'}]}});}
   return send({...catalog(),sync_result:{status:'partial',units:[{skill_id:alpha.id,status:'succeeded'},{skill_id:beta.id,status:'failed'}]}});
  }
  return send({detail:{message:'unexpected fixture route'}},404);
 });
 await page.goto('http://rdos.test/');await page.evaluate(async user=>{h.beginSession(user);await h.refresh();},admin);
 await page.click('[data-page="knowledge"]');await page.waitForFunction(()=>!!h.knowledge.data);
 assert.equal(await page.locator('[data-knowledge-category]').count(),2);assert.equal(await page.locator('[data-knowledge-category="context"]').count(),0);
 assert.equal(await page.locator('[data-knowledge-category="selected_rag"]').getAttribute('aria-selected'),'true');
 await page.click('[data-knowledge-open="rag"]');await page.waitForFunction(()=>!!h.knowledge.detail);assert.match(await page.locator('#knowledge-content').innerText(),/RAG仍按原资料展示/);
 pass('Context removed; Selected RAG remains the default and readable');
 await page.click('[data-knowledge-category="shared_skills"]');assert.equal(await page.locator('#knowledge-list [data-knowledge-open]').count(),2);
 assert.equal(await page.locator('#knowledge-list [data-knowledge-open="reference"]').count(),0);assert.match(await page.locator('#knowledge-source').innerText(),/2 个 Skill/);
 await page.click('[data-knowledge-open="skill_alpha"]');await page.waitForFunction(()=>h.knowledge.detail?.item.id==='skill_alpha');
 assert.match(await page.locator('#knowledge-content').innerText(),/本包主说明/);assert.equal(await page.evaluate(()=>globalThis.packageInjected),undefined);
 await page.locator('.knowledge-package-files>summary').click();assert.match(await page.locator('.knowledge-file-tree').innerText(),/references/);assert.match(await page.locator('.knowledge-file-tree').innerText(),/事实 \/ 判断.md/);
 pass('One folder is one Skill; the package opens its own instructions and nested files');
 await page.click('[data-knowledge-file="reference"]');await page.waitForFunction(()=>h.knowledge.fileDetail?.item.id==='reference');
 const readCall=calls.filter(call=>call.pathname.endsWith('/items/reference')).at(-1);
 assert.equal(readCall.version,'ref-v1');assert.equal(readCall.skill_id,'skill_alpha');assert.equal(readCall.package_version,'package:alpha-v1');assert.match(await page.locator('#knowledge-content').innerText(),/FROZEN_REFERENCE_VERSION_1/);
 assert.equal(await page.locator('#knowledge-list [data-knowledge-open]').count(),2);
 const before=calls.length;await page.evaluate(()=>h.openKnowledgeFile('outside-package'));assert.equal(calls.length,before);
 pass('Child reads bind to the package manifest version; unknown files cannot be opened');
 await page.click('[data-knowledge-file="script"]');await page.waitForFunction(()=>h.knowledge.fileDetail?.item.id==='script');assert.match(await page.locator('#knowledge-content').innerText(),/未执行/);
 await page.click('[data-knowledge-main]');assert.match(await page.locator('#knowledge-content').innerText(),/本包主说明/);
 const siblingVersion=beta.version;failSync=true;await page.click('[data-knowledge-skill="skill_alpha"]');await page.waitForFunction(()=>!h.knowledge.syncing);
 assert.equal(calls.filter(call=>call.pathname.endsWith('/knowledge/sync')).at(-1).body.skill_id,'skill_alpha');assert.equal(alpha.version,'package:alpha-v1');assert.equal(beta.version,siblingVersion);assert.match(await page.locator('#toast').innerText(),/未同步/);
 failSync=false;await page.click('[data-knowledge-skill="skill_alpha"]');await page.waitForFunction(()=>!h.knowledge.syncing);assert.equal(alpha.version,'package:alpha-v2');assert.equal(beta.version,siblingVersion);
 pass('Single-Skill sync targets its stable package ID and preserves prior versions on failure');
 await page.locator('#knowledge-heading-controls [data-knowledge-sync="shared_skills"]').click();await page.waitForFunction(()=>!h.knowledge.syncing);assert.match(await page.locator('#toast').innerText(),/未同步/);
 await page.click('[data-knowledge-open="skill_beta"]');await page.waitForFunction(()=>h.knowledge.detail?.item.id==='skill_beta');assert.match(await page.locator('#knowledge-content').innerText(),/未找到 SKILL.md/);assert.equal(await page.locator('.knowledge-package-files').getAttribute('open'),'');
 await page.click('[data-knowledge-file="archive"]');await page.waitForFunction(()=>h.knowledge.fileDetail?.item.id==='archive');assert.match(await page.locator('#knowledge-content').innerText(),/正文尚未解析/);
 pass('Partial sync stays explicit; folders without SKILL.md remain visible with their files');
 await page.click('[data-knowledge-open="skill_alpha"]');await page.waitForFunction(()=>h.knowledge.detail?.item.id==='skill_alpha');await page.locator('.knowledge-package-files>summary').click();
 delayReference=true;await page.click('[data-knowledge-file="reference"]');await page.waitForFunction(()=>h.knowledge.fileId==='reference');
 await page.click('[data-knowledge-open="skill_beta"]');await page.waitForFunction(()=>h.knowledge.detail?.item.id==='skill_beta');releaseReference();await page.waitForTimeout(100);assert.doesNotMatch(await page.locator('#knowledge-content').innerText(),/FROZEN_REFERENCE_VERSION_1/);
 pass('Late child-file responses cannot overwrite another Skill');
 await page.evaluate(async user=>{h.beginSession(user);await h.refresh();},reader);activeUser=reader;
 await page.click('[data-page="knowledge"]');await page.waitForFunction(()=>!!h.knowledge.data);await page.click('[data-knowledge-category="shared_skills"]');await page.click('[data-knowledge-open="skill_alpha"]');await page.waitForFunction(()=>!!h.knowledge.detail);
 assert.equal(await page.locator('[data-knowledge-sync]').count(),0);assert.equal(await page.locator('[data-knowledge-category="context"]').count(),0);
 pass('Researcher package browsing stays read-only');
 assert.deepEqual(report.errors,[]);fs.mkdirSync(out,{recursive:true});await page.screenshot({path:out+'/skill-package.png',fullPage:true});report.finished_at=new Date().toISOString();fs.writeFileSync(out+'/report.json',JSON.stringify(report,null,2));await browser.close();
})().catch(async error=>{report.failure=String(error.stack||error);fs.mkdirSync(out,{recursive:true});fs.writeFileSync(out+'/report.json',JSON.stringify(report,null,2));console.error(error);if(browser)await browser.close();process.exitCode=1;});
