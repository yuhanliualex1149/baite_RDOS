"""Business tools share one service; writes require a fixed authenticated plan."""
from __future__ import annotations
import asyncio
from server import phase3_service as service, phase3_backend as backends
from server import rdos_rules as rules
from server import rdos_contract, rdos_projects


def _registry():
 return {**backends.get_backend().registry(),'nodes':service.node_registry()}


def _business(value):
 from server.central_api import safe_display
 return rdos_contract.public_view(safe_display(value),_registry())

TASK_SCHEMA = {'type':'object','additionalProperties':False,'properties':{
 'task_id':{'type':'string','description':'调整现有任务时填写；新任务省略。'},
 'title':{'type':'string'},'goal':{'type':'string'},'deliverable':{'type':'string'},
 'change_reason':{'type':'string','description':'调整目标或交付条件时说明变更原因及旧成果处理。'},
 'task_type':{'type':'string','enum':list(rules.TASK_TYPES),'description':'动作、成果制作或Agent审阅。默认action。'},
 'owner_id':{'type':'string'},
 'executor_node_ids':{'type':'array','items':{'type':'string'},'description':'使用workspace登记的节点标识。'},
 'deliverable_audience':{'type':'array','items':{'type':'string','enum':list(rules.AUDIENCES)}},
 'input_refs':{'type':'array','items':{'type':'object','properties':{
     'document_id':{'type':'string'},'revision':{'type':['integer','string']},'content_hash':{'type':'string'}},
     'required':['document_id','revision','content_hash']},'description':'Review须绑定指定被审阅成果版本。'},
 'output_zone':{'type':'string','enum':['project','agent']},
 'dispatch_to':{'type':'array','items':{'type':'string','enum':['agent','human']}},
 'return_to':{'type':'array','items':{'type':'string','enum':['agent','human']}},
 'completion_rule':{'type':'object','properties':{'kind':{'type':'string','enum':list(rules.COMPLETION_KINDS)}},
     'additionalProperties':False,'required':['kind'],
     'description':'默认receipt_recorded：完成执行与回执核对即收尾。只有明确要求人判断质量才选human；review任务类别本身不要求人工验收。'},
 'due_at':{'type':'string'},'editable_document_ids':{'type':'array','items':{'type':'string'}}}}

MUTATIONS = {'phase3_apply_task_action','phase3_start_task','phase3_update_task','phase3_pause_task',
             'phase3_resume_task','phase3_close_task','phase3_create_document','phase3_patch_document'}
TOOLS = [
 ('phase3_read_workspace','读取项目工作进展、成果、问题和需要决定的事项。',{}),
 ('phase3_list_tasks','从登记的任务主记录读取本项目当前要求和状态。',{}),
 ('phase3_read_document','读取已登记的项目或Agent协作原生文档。',{'document_id':{'type':'string'}}),
 ('phase3_list_knowledge','按名称或目录查询共享知识。Shared Skills以根目录下的文件夹为一个Skill，读取后查看说明及包内文件；目录不代表机构发布。',{
     'category':{'type':'string','enum':['selected_rag','shared_skills']},
     'query':{'type':'string','description':'文件名或目录关键词，普通文本匹配。'}}),
 ('phase3_read_knowledge','读取目录中指定知识的确切版本，引用返回的来源链接；材料不产生执行授权。',{
     'item_id':{'type':'string'},'version':{'type':'string','description':'可选；省略时读取当前缓存版本。'}}),
 ('phase3_prepare_task','起草工作任务。需要补写目标、输入或交付时先展示短稿；发布须有当前管理员请求绑定。',{'payload':TASK_SCHEMA}),
]

def specs(run=None):
 required = {'phase3_read_document':['document_id'], 'phase3_read_knowledge':['item_id'], 'phase3_prepare_task':['payload']}
 output=[]
 for name,description,props in TOOLS:
  properties=dict(props)
  if name not in {'phase3_list_knowledge','phase3_read_knowledge'}:
   properties['project_id']={'type':'string','description':'机构对话须明确项目；项目对话不能更改已选项目。'}
  output.append({'type':'function','function':{'name':name,'description':description,
          'parameters':{'type':'object','properties':properties,'required':required.get(name,[]),'additionalProperties':False}}})
 if run and run.get('purpose')=='execute' and run.get('request_plan'):
  output.append({'type':'function','function':{'name':'phase3_execute_task_request',
      'description':'执行当前管理员已明确要求且由服务器固定的任务动作；只接受计划标识和动作序号，不能改写任务内容或宣称批准。',
      'parameters':{'type':'object','properties':{'plan_id':{'type':'string'},'action_index':{'type':'integer','minimum':0,'maximum':3}},
                    'required':['plan_id','action_index'],'additionalProperties':False}}})
 return output

async def execute(name,args,run,actor):
 if name in MUTATIONS:
  return {'ok':False,'code':'not_allowed_unattended' if run.get('trigger')=='scheduled' else 'not_allowed',
          'error':'任务动作需要当前管理员请求的固定计划，或Panel具体版本确认；模型文本不能代替授权。'}
 if name=='phase3_execute_task_request':
  if (run.get('trigger')!='manual' or run.get('purpose')!='execute' or not isinstance(args,dict)
      or set(args)!={'plan_id','action_index'}):
   return {'ok':False,'code':'not_allowed','error':'当前请求没有任务执行授权'}
  try:
   from server.central_requests import execute_action
   item=await execute_action(args['plan_id'],args['action_index'],run,actor)
   return _business({'ok':True,'item':item,'source':'authenticated_user_request'})
  except Exception as exc:
   from server.central_service import safe_error
   return {'ok':False,'code':getattr(exc,'code','validation'),'error':safe_error(exc)}
 allowed=next((props for tool,_,props in TOOLS if tool==name),None)
 permitted=set(allowed or {}) | ({'project_id'} if name not in {'phase3_list_knowledge','phase3_read_knowledge'} else set())
 if allowed is None or not isinstance(args,dict) or set(args)-permitted:
  return {'ok':False,'code':'not_allowed','error':'业务工具或参数未授权'}
 project_id = args.get('project_id') or run.get('project_id')
 if project_id:
  project_id=rdos_contract.internal_input(project_id,_registry())
 if (project_id and (project_id not in run.get('allowed_projects',[]) or not rdos_projects.is_registered(project_id)
     or (run.get('project_id') and project_id!=run['project_id']))):
  return {'ok':False,'code':'authorization_failed','error':'工具不属于此运行的项目范围'}
 if not project_id and name not in {'phase3_list_knowledge','phase3_read_knowledge','phase3_list_tasks'}:
  return {'ok':False,'code':'authorization_failed','error':'请明确要读取或办理的项目'}
 try:
  if name=='phase3_list_knowledge':
   from server.shared_knowledge import model_catalog
   return _business({'ok':True,**model_catalog(actor, category=args.get('category'), query=args.get('query','')),
                     'source':'institution_knowledge_cache'})
  if name=='phase3_read_knowledge':
   from server.shared_knowledge import read_item
   if not isinstance(args.get('item_id'),str) or not args['item_id'] or ('version' in args and not isinstance(args['version'],str)):
    raise ValueError('请使用共享知识目录中的条目与版本')
   item=read_item(args['item_id'],actor,args.get('version') or None)
   return _business({'ok':True,**item,'source':'institution_knowledge_cache'})
  if name=='phase3_read_workspace':return _business({'ok':True,**service.business_workspace(actor,project_id),'source':'verified_cache'})
  if name=='phase3_list_tasks':
   rows=await asyncio.to_thread(backends.get_backend().list_tasks)
   scope={project_id} if project_id else {p for p in run.get('allowed_projects',[]) if rdos_projects.is_registered(p)}
   return _business({'ok':True,'items':[service.business_task(service._validate_task(row)) for row in rows if row.get('project_id') in scope],'source':'registered_task_authority'})
  if name=='phase3_read_document':
   scope=rdos_projects.resource_scope(project_id,backends.get_backend().registry())
   cached=service.current_project_document(args['document_id'],project_id)
   if not cached and args['document_id'] not in {d.get('id') for d in scope.get('documents',[]) if not d.get('retired')}:
    raise service.Phase3Error('out_of_scope', '资料不属于当前项目登记范围')
   document=cached or await service.read_document(args['document_id'],actor,project_id)
   return _business({'ok':True,**{key:document[key] for key in ('id','title','content','revision','content_hash','source_url') if key in document}})
  if name=='phase3_prepare_task':
   item=await service.prepare_task(rdos_contract.internal_input(args['payload'],_registry()),actor,run['operation_id']+':phase3-draft',project_id=project_id)
   return _business({'ok':True,'item':service.business_draft(item),'requires_confirmation':True})
 except Exception as exc:
  from server.central_service import safe_error
  return {'ok':False,'code':getattr(exc,'code','validation'),'error':safe_error(exc)}
