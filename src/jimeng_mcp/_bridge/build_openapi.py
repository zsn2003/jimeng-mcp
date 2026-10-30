from pathlib import Path
import json
import action_registry
root=Path(__file__).resolve().parent
schemas=action_registry.request_schemas()
error={'description':'Error','content':{'application/json':{'schema':{'type':'object','properties':{'error':{'type':'object','required':['code','message'],'properties':{'code':{'type':'string'},'message':{'type':'string'}}}}}}}}
job={'type':'object','required':['id','status','createdAt','updatedAt'],'properties':{'id':{'type':'string'},'status':{'type':'string','enum':['QUEUED','RUNNING','COMPLETED','FAILED','UNKNOWN'],'description':'COMPLETED means adapter call returned; inspect result.upstream.ok and media states.'},'createdAt':{'type':'number'},'updatedAt':{'type':'number'},'recovery':{'type':['object','null']},'result':{'type':['object','null']},'error':{'type':['object','null']}}}
jresp={'description':'Persisted job','content':{'application/json':{'schema':{'$ref':'#/components/schemas/Job'}}}}
spec={'openapi':'3.1.0','info':{'title':'Jimeng MCP Local Bridge','version':'0.6.0','description':'Authenticated fixed operations for the official canvas CLI and privately configured studio web session. The runtime capabilities endpoint declares enabled operations, current write policy, and credit policy; the separate studio catalog maps feature coverage and acceptance evidence; it is not a dispatch table.'},'servers':[{'url':'http://127.0.0.1:8773','description':'Local authenticated HTTP backend. The local launcher configures its port and MCP transport.'}],'security':[{'bearerAuth':[]}],'paths':{'/healthz':{'get':{'operationId':'health','security':[],'responses':{'200':{'description':'Alive'}}}},'/v1/capabilities':{'get':{'operationId':'capabilities','responses':{'200':{'description':'Implemented fixed actions, current write and credit policy, and explicitly unavailable features'},'401':error}}},'/v1/studio/catalog':{'get':{'operationId':'studioCoverageCatalog','description':'Feature coverage, mapped actions, acceptance evidence, and remaining gaps.','responses':{'200':{'description':'Coverage and acceptance registry'},'401':error}}},'/v1/jobs':{'post':{'operationId':'submitJob','parameters':[{'in':'header','name':'Idempotency-Key','required':True,'schema':{'type':'string','minLength':1,'maxLength':200}}],'requestBody':{'required':True,'content':{'application/json':{'schema':{'oneOf':list(schemas.values())}}}},'responses':{'202':jresp,'200':jresp,'401':error,'409':error,'422':error}}},'/v1/jobs/{id}':{'get':{'operationId':'getJob','parameters':[{'in':'path','name':'id','required':True,'schema':{'type':'string','pattern':'^[0-9a-f]{32}$'}}],'responses':{'200':jresp,'401':error,'404':error}}}},'components':{'securitySchemes':{'bearerAuth':{'type':'http','scheme':'bearer'}},'schemas':{'Job':job}}}
job['properties'].update({
 'operationOutcome':{'type':'string','enum':['accepted','running','succeeded','rejected','partial','unknown']},
 'reconciliationRequired':{'type':'boolean'},
 'dispatchAttempted':{'type':['boolean','null'],'description':'Whether an upstream write/generation was attempted; null means unknown.'}})
job['properties']['status']['description']='Durable bridge execution state. COMPLETED is adapter call return; operationOutcome describes business certainty. Media completion requires actual upstream resource status.'
def param(name,kind='string',location='query',required=False,**extra):
 return {'name':name,'in':location,'required':required,'schema':{'type':kind,**extra}}
json_response=lambda description:{'description':description,'content':{'application/json':{'schema':{'type':'object'}}}}
spec['paths']['/v1/jobs']['get']={'operationId':'listJobs','parameters':[
 param('limit','integer',minimum=1,maximum=100),param('cursor'),param('status',enum=['QUEUED','RUNNING','COMPLETED','FAILED','UNKNOWN']),param('action'),param('projectId')],
 'responses':{'200':json_response('Bounded task summaries without prompt or media bytes'),'401':error,'400':error}}
spec['paths']['/v1/queue']={'get':{'operationId':'queueSummary','responses':{'200':json_response('Queue counts with one write worker and two fixed read workers'),'401':error}}}
spec['paths']['/v1/jobs/{id}/cancel']={'post':{'operationId':'cancelQueuedJob','parameters':[param('id',location='path',required=True,pattern='^[0-9a-f]{32}$')],
 'requestBody':{'required':True,'content':{'application/json':{'schema':{'type':'object','additionalProperties':False}}}},
 'responses':{'200':json_response('Only QUEUED jobs are cancelled; no upstream cancellation is attempted'),'409':error,'401':error,'404':error}}}
artifact_params=[param('artifactId',location='path',required=True,pattern='^[0-9a-f-]{36}$'),param('projectId',required=True,pattern='^[0-9a-f-]{36}$'),param('kind',enum=['resource','director'])]
spec['paths']['/v1/artifacts/{artifactId}/manifest']={'get':{'operationId':'artifactManifest','parameters':artifact_params,
 'responses':{'200':json_response('Verified metadata and relative authenticated download route; no private path or token'),'401':error,'404':error}}}
spec['paths']['/v1/artifacts/{artifactId}']={'get':{'operationId':'downloadManagedArtifact','parameters':[*artifact_params,param('Range',location='header')],
 'description':'Authenticated binary stream; one file descriptor is verified and streamed. Does not create jobs or persist Base64.',
 'responses':{'200':{'description':'Full artifact','content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}},
 '206':{'description':'Single byte range'},'401':error,'404':error,'416':error}}}
(root/'openapi.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'callableActions':len(schemas),'actions':list(schemas)},ensure_ascii=False))
