/* Business UI behavior checks use in-memory DOM/API doubles; they are not browser acceptance. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const html=fs.readFileSync(path.join(__dirname,'../server/static/central.html'),'utf8');
const script=fs.readFileSync(path.join(__dirname,'../server/static/central.js'),'utf8');
const index=script.lastIndexOf('  (async()=>{try{');
assert(index>0,'Disable network bootstrap before loading the test harness');
const source=script.slice(0,index)+`globalThis.businessUI={state,phase3,renderPhase3,showPhase3Task,phase3Requirements,runContent,renderNavigation,renderMessages,renderPeople,developerMode,showPhase3Action,maintenanceCard,deletedDrafts,rodos,approvePeer,peerBindingButton,setPeerTestHooks(f){api=f;loadRodos=async()=>{};}};})();`;
function setup(developer=false){
  const nodes=new Map(),listeners=new Map();let clipboard='';
  class Node{
    constructor(id){this.id=id;this.dataset={};this.value='';this.textContent='';this.disabled=false;this.checked=false;this.options=[];this.attributes=[];this.scrollTop=0;this.events={};this.classes=new Set();this.isConnected=true;this.classList={toggle:(k,on)=>on?this.classes.add(k):this.classes.delete(k),add:k=>this.classes.add(k),remove:k=>this.classes.delete(k),contains:k=>this.classes.has(k)};}
    set innerHTML(value){this.html=value;this.options=[...value.matchAll(/<option value="([^"]*)"/g)].map(m=>({value:m[1],selected:false}));}
    get innerHTML(){return this.html||'';}
    addEventListener(k,f){this.events[k]=f;} querySelectorAll(){return [];} querySelector(){return null;} contains(){return false;} getAttribute(){return '';} setAttribute(){} removeAttribute(){} reset(){} showModal(){this.open=true;} close(){this.open=false;} focus(){}
  }
  const node=id=>{if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id);};
  const document={body:node('body'),baseURI:'http://127.0.0.1:18760/',activeElement:null,getElementById:node,querySelectorAll:()=>[],addEventListener:(k,f)=>{const previous=listeners.get(k);listeners.set(k,previous?event=>{previous(event);f(event);}:f);}};
  const location={pathname:developer?'/dev':'/',href:'http://127.0.0.1:18760'+(developer?'/dev':'/'),origin:'http://127.0.0.1:18760'};
  const fetch=async (url,options)=>{assert.equal(url,'api/central/phase3/nodes/research/onboarding');assert.equal(options.method,'POST');return {ok:true,status:200,text:async()=>JSON.stringify({instruction:'请读取 http://127.0.0.1:18760/nodes/research/start ，本节点：research。按当前配置任务完成接入。',task:{task_id:'setup-research'}})};};
  const context=vm.createContext({document,location,window:{addEventListener(){}},URL,crypto:webcrypto,navigator:{clipboard:{writeText:async t=>clipboard=t}},setTimeout:()=>1,clearTimeout(){},setInterval(){},clearInterval(){},sessionStorage:{getItem(){},setItem(){}},AbortController,fetch,console});
  vm.runInContext(source,context);const h=context.businessUI;
  h.state.user={id:'owner',display_name:'管理员',identity_mode:'role_test',roles:['admin']};
  h.state.overview={users:[h.state.user,...['a','b','c'].map(id=>({id:'member-'+id,display_name:'研发成员'+id,username:'member-'+id,roles:['researcher'],enabled:true}))]};
  h.phase3.data={nodes:[{node_id:'internal-a',public_id:'codex',display_name:'Codex',member_id:'member-a'},{node_id:'internal-b',public_id:'workbuddy',display_name:'WorkBuddy',member_id:'member-b'},{node_id:'internal-c',public_id:'research',display_name:'研究节点',member_id:'member-c'}],scope:{zones:{}},tasks:[],documents:[],events:[],drafts:[],project:{name:'工作协作'}};
  return {h,node,listeners,clipboard:()=>clipboard};
}
let passed=0;
const report={mode:'in-memory DOM/API doubles, not browser acceptance',started_at:new Date().toISOString(),checks:[]};
function test(name,fn){return Promise.resolve().then(fn).then(()=>{passed++;report.checks.push({name,passed:true});console.log('PASS '+name);}).catch(error=>{report.checks.push({name,passed:false,error:error.message});throw error;});}
(async()=>{
  await test('normal navigation cannot open developer work',()=>{const {h,node}=setup();h.state.page='work';h.renderNavigation();assert.equal(h.state.page,'projects');assert.equal(node('page-title').textContent,'项目管理');});
  await test('registered nodes populate associated member cards and task assignment without a two-node limit',()=>{const {h,node}=setup();h.renderPhase3();const cards=node('people-list').innerHTML.split('</article>'),third=cards.find(card=>card.includes('data-key="user:member-c"')),admin=cards.find(card=>card.includes('data-key="user:owner"'));assert.match(third,/研究节点/);assert.match(third,/data-phase3-copy="research"/);assert.doesNotMatch(admin,/data-phase3-copy=/);assert.equal((node('people-list').innerHTML.match(/data-phase3-copy=/g)||[]).length,3);assert.doesNotMatch(html,/id="phase3-nodes"/);h.showPhase3Task();assert.equal(node('phase3-task-node').options.length,3);assert.equal(node('phase3-task-node').value,'codex');});
  await test('new action defaults to automatic receipt completion and distinct audience',()=>{const {h,node}=setup();h.showPhase3Task();assert.equal(node('phase3-task-completion').value,'receipt_recorded');assert.equal(node('phase3-task-type').value,'action');assert.equal(node('phase3-task-audience').value,'admin,project_members');});
  await test('automatic completion never creates human quality acceptance',()=>{const {h,node}=setup();h.phase3.data.tasks=[{task_id:'done',title:'更新工作方式',business_status:'closed',requirements_version:2,record_revision:3,completion_rule:{kind:'receipt_recorded'},task_type:'action',result_status:'completed',receipt_status:'verified',quality_status:'not_evaluated',execution_result:{summary:'工作方式已更新。'},execution:{private_trace:'DO_NOT_RENDER'}}];node('phase3-show-closed').checked=true;node('phase3-show-closed').events.change();h.renderPhase3();assert.match(node('phase3-tasks').innerHTML,/动作完成|已写回并核对|未作质量判断/);assert.doesNotMatch(node('phase3-tasks').innerHTML,/DO_NOT_RENDER|data-phase3-action="accept_close"/);assert.match(node('phase3-decisions').innerHTML,/没有需要你处理/);});
  await test('Agent Review type alone does not create a quality approval task',()=>{const {h,node}=setup();h.phase3.data.tasks=[{task_id:'review',title:'审阅成果',task_type:'review',business_status:'pending_review',requirements_version:1,completion_rule:{kind:'receipt_recorded'}}];h.renderPhase3();assert.doesNotMatch(node('phase3-tasks').innerHTML,/data-phase3-action="accept_close"/);assert.doesNotMatch(node('phase3-decisions').innerHTML,/审阅指定版本/);});
  await test('legacy human completion no longer exposes quality actions',()=>{const {h,node}=setup();h.phase3.data.tasks=[{task_id:'quality',title:'课程成果',business_status:'pending_review',requirements_version:1,completion_rule:{kind:'human'}}];h.renderPhase3();assert.doesNotMatch(node('phase3-tasks').innerHTML,/accept_close|质量：|质量审阅/);assert.match(node('phase3-tasks').innerHTML,/data-phase3-action="manual_complete"/);assert.doesNotMatch(node('phase3-decisions').innerHTML,/质量/);});
  await test('business cards escape summaries and omit technical raw data',()=>{const {h,node}=setup();h.phase3.data.tasks=[{task_id:'t',title:'正常任务',business_status:'active',requirements_version:1,result_status:'blocked',execution_result:{summary:'<script>alert(1)</script>'},completion_rule:{kind:'receipt_recorded'}}];h.phase3.data.quarantine=[{raw:'DO_NOT_RENDER'}];h.renderPhase3();assert.match(node('phase3-tasks').innerHTML,/&lt;script&gt;/);assert.doesNotMatch(node('phase3-decisions').innerHTML,/DO_NOT_RENDER|<script>/);assert.doesNotMatch(h.runContent({status:'failed',error:'处理失败',steps:[{secret:'DO_NOT_RENDER'}]}),/DO_NOT_RENDER|工具与技术记录/);});
  await test('short node instructions use the public onboarding route and business context',async()=>{const {h,node,listeners,clipboard}=setup();const button=node('copy');button.dataset.phase3Copy='research';button.closest=()=>button;button.tagName='BUTTON';listeners.get('click')({target:button});await new Promise(resolve=>setImmediate(resolve));assert.match(clipboard(),/http:\/\/127\.0\.0\.1:18760\/nodes\/research\/start/,node('member-management-error').textContent);assert.doesNotMatch(clipboard(),/实验|测试|role_test|phase3|lab|要求版本|提交结构/);});
  await test('developer entry retains isolated diagnostic detail',()=>{const {h,node}=setup(true);assert.equal(h.developerMode,true);h.phase3.data.quarantine=[{raw:'DEVELOPER_ONLY'}];h.renderPhase3();assert.match(node('phase3-decisions').innerHTML,/DEVELOPER_ONLY/);h.state.page='work';h.renderNavigation();assert.equal(h.state.page,'work');});
  await test('manual periodic completion explains ending the entire task',()=>{const {h,node}=setup();h.showPhase3Action({task_id:'weekly',title:'周报',requirements_version:1,record_revision:1,periodicity:{unit:'week'}},'manual_complete');assert.match(node('phase3-action-preview').innerHTML,/整个周期任务/);assert.match(node('phase3-action-preview').innerHTML,/共用定时安排不变/);assert.equal(node('phase3-action-reason').required,false);});
  await test('manual completion preserves actual node evidence',()=>{const {h,node}=setup();h.phase3.data.tasks=[{task_id:'manual',title:'配置',business_status:'closed',close_reason:'manual_completed',requirements_version:1,result_status:'partial',receipt_status:'pending'}];node('phase3-show-closed').checked=true;node('phase3-show-closed').events.change();h.renderPhase3();assert.match(node('phase3-tasks').innerHTML,/管理员已标记完成/);assert.doesNotMatch(node('phase3-tasks').innerHTML,/已写回并核对/);});
  await test('deleted drafts offer restore without publish',()=>{const {h}=setup();const text=h.deletedDrafts('phase3',[{id:'d',status:'deleted',payload:{title:'旧草稿'},draft_hash:'x'}]);assert.match(text,/恢复草稿/);assert.doesNotMatch(text,/确认并开始/);});
  await test('refresh cards have no details while inspection keeps evidence',()=>{const {h}=setup();for(const status of ['running','succeeded','partial'])assert.doesNotMatch(h.maintenanceCard({id:'refresh',kind:'refresh',status},true),/查看详情|<details>/);assert.match(h.maintenanceCard({id:'inspect',kind:'inspection',status:'succeeded'}),/查看详情/);});
  await test('registration progress displays no human approval or confirmation code',()=>{
    const {h}=setup();
    for(const [status,label] of [['pending','待自动登记'],['registry_pending','正在登记']]){
      h.rodos.nodes=[{node_id:'node-yuhan',bound:false,pending_requests:[{request_id:'original-request',request_hash:'original-hash',confirmation_code:'DO_NOT_PROJECT_CODE',status}]}];
      const text=h.peerBindingButton('node-yuhan');assert.match(text,new RegExp(label));
      assert.doesNotMatch(text,/data-peer-approve|确认节点归属|确认绑定|登记码|DO_NOT_PROJECT_CODE/);
    }
  });
  await test('automatic registration and node readiness remain separate without approval controls',()=>{
    const {h}=setup();h.rodos.nodes=[{node_id:'node-yuhan',bound:true,ready:false,pending_requests:[]}];
    assert.match(h.peerBindingButton('node-yuhan'),/登记完成（互派待就绪）/);
    h.rodos.nodes[0].ready=true;assert.match(h.peerBindingButton('node-yuhan'),/互派已启用/);
    assert.doesNotMatch(h.peerBindingButton('node-yuhan'),/data-peer-approve|确认节点归属/);
  });
  await test('missing peer enrollment neither means onboarding failure nor borrows another node request',()=>{
    const {h}=setup();h.rodos.nodes=[{node_id:'home-node',bound:false,pending_requests:[]},{node_id:'office-node',bound:false,pending_requests:[{request_id:'office-request',status:'pending',confirmation_code:'1234-5678-9ABC'}]}];
    const home=h.peerBindingButton('home-node'),office=h.peerBindingButton('office-node');
    assert.match(home,/互派未启用/);assert.doesNotMatch(home,/接入失败|data-peer-approve|office-request/);
    assert.match(office,/待自动登记/);assert.doesNotMatch(office,/data-peer-approve|office-request|登记码/);
  });
  console.log(`${passed} business frontend behavior checks passed`);
})().catch(e=>{report.failure=e.message;console.error(e);process.exitCode=1;}).finally(()=>{report.finished_at=new Date().toISOString();report.passed=passed===16;const evidence=path.resolve(__dirname,'../../evidence/members_knowledge_ui');fs.mkdirSync(evidence,{recursive:true});fs.writeFileSync(path.join(evidence,'business-dom-report.json'),JSON.stringify(report,null,2));});
