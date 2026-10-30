# Fixtures use synthetic identities and material; no user account data.
"""No generation: verify extension identity, source policy and fixed read boundary."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
import canvas_web_actions as extension

PROJECT='1000000b-0000-4000-8000-000000000002'
SOURCE='node_0000000001'
TARGET='node_0000000002'
SOURCE_RESOURCE='30000000-0000-4000-8000-000000000002'
RESOURCE='30000000-0000-4000-8000-000000000001'
SUBMIT='20000000-0000-4000-8000-000000000001'
SESSION={'cookie':'secret-cookie-value','userAgent':'test-private-agent','headers':{},'actionHeaders':{}}


def payload(**changes):
    result={'action':'video.extend','projectId':PROJECT,'sourceNodeId':SOURCE,'sourceResourceId':SOURCE_RESOURCE,
            'targetNodeId':TARGET,'prompt':'纸飞机保持运动方向继续飞行。'}
    result.update(changes);return result


def source_resource(**changes):
    result={'resource_id':SOURCE_RESOURCE,'type':2,'status':1000,'generation_kind':3,
            'video':{'vid':'internal-source-video-id','width':1920,'height':1080,'resolution':'1080p','duration':4.05,
                     'model_name':'seedance_2.5_draft','gen':{'aspect_ratio':'16:9'}}}
    result.update(changes);return result


def source_node():
    return {'nodeId':SOURCE,'type':'video','status':'success','resources':[{'resourceId':SOURCE_RESOURCE}]}


def target_node():
    return {'nodeId':TARGET,'type':'video','status':'empty','resources':[],
            'upstreamNodeIds':[SOURCE],'executionDependencyNodeIds':[SOURCE]}


def draft_snapshot(prompt=None, version=11):
    slot='40000000-0000-4000-8000-000000000001'
    generation={'contentSettings':{'aspectRatio':'16:9','durationSeconds':5,'mode':'videoExtend',
                 'modelName':'seedance_2.5','resolution':'1080p'},'kind':'video','outputCount':1,
                'parts':[{'type':'extension','extension_type':'node','object_id':slot},
                         {'type':'text','text':prompt or payload()['prompt']}],
                'references':[{'type':'extension','extensionType':'node','role':'source','slotId':slot,
                               'nodeId':SOURCE,'sourceInstanceId':SOURCE}]}
    document={'schemaVersion':1,'draftVersion':version,
              'nodes':[{'id':SOURCE,'type':'video','data':{'resourceId':SOURCE_RESOURCE}},
                       {'id':TARGET,'type':'video','data':{'resourceBatches':[],'generationDraft':generation}}],
              'edges':[{'id':'edge_0123456789','source':SOURCE,'target':TARGET,'type':'reference'}]}
    return {'version':version,'dataVersion':extension.DRAFT_DATA_VERSION,
            'schemaProfile':extension.DRAFT_SCHEMA_PROFILE,'document':document}


def existing_extension():
    return {'resource_id':RESOURCE,'type':2,'status':1000,'generation_kind':1,'submit_id':SUBMIT,
            'reference_sources':[{'material_idx':0,'node_id':SOURCE,'resource_id':SOURCE_RESOURCE}],
            'video':{'generation_mode':9,'model_name':'seedance_2.5','gen':{'duration_ms':5000},
                     'width':1920,'height':1080,'resolution':'1080p','duration':9.079,'fps':30}}


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'JIMENG_READ_ONLY':'0','JIMENG_MAX_CREDITS':'unlimited'})
        self.env.start();self.addCleanup(self.env.stop)
        self.session=patch.object(extension.web_actions,'_load_session',return_value=SESSION)
        self.session.start();self.addCleanup(self.session.stop)

    def execute(self, p=None, resource=None, target=None, snapshot=None):
        snapshot=snapshot or draft_snapshot()
        if resource and resource.get('generation_kind')==2:
            snapshot['document']['nodes'][1]['data']['generationDraft']['contentSettings']['resolution']='480p'
        with patch.object(extension,'_node',side_effect=[source_node(),target or target_node()]), \
             patch.object(extension,'_resource',return_value=resource or source_resource()), \
             patch.object(extension,'_commerce',return_value={'benefit_type':'seedance_25_1080p_output'}), \
             patch.object(extension,'_draft',return_value=snapshot), \
             patch.object(extension.web_actions,'_send') as send:
            result=extension.execute(p or payload(),'test-job',invoke=lambda args:None)
        send.assert_not_called()
        return result

    def test_default_preflight_accepts_promoted_final_even_when_source_model_has_draft_name(self):
        result=self.execute()
        self.assertTrue(result['upstream']['ok'])
        self.assertTrue(result['dryRun'])
        self.assertFalse(result['generationSubmitted'])
        self.assertTrue(result['plan']['rawTargetDraftVerified'])
        self.assertEqual(result['plan']['canonicalDraft']['version'],11)
        self.assertFalse(result['plan']['targetAutoCreated'])
        self.assertEqual(result['plan']['generationKind'],'ordinary')
        self.assertEqual(result['plan']['resolution'],'1080p')
        self.assertNotIn('internal-source-video-id',json.dumps(result))

    def test_official_node_read_uses_result_found_contract(self):
        response={'ok':True,'data':{'nodes':[{'result':'FOUND','node':source_node()}],'draftVersion':11}}
        self.assertEqual(extension._node(PROJECT,SOURCE,lambda args:response)['nodeId'],SOURCE)

    def test_historical_preflight_cannot_mark_live_submission_verified(self):
        calls=[]
        def verified(action, variant=None):
            calls.append((action,variant))
            return [{'variant':'existing_mode9_read'}] if action=='video.extend.status' else []
        with patch.dict(sys.modules,{'acceptance_registry':SimpleNamespace(verified=verified)}):
            actions=extension.capabilities()['actions']
        self.assertFalse(actions['video.extend']['serverExecutionVerified'])
        self.assertTrue(actions['video.extend.status']['serverExecutionVerified'])
        self.assertIn(('video.extend','prepared_target_submission'),calls)

    def test_sample_rejected_without_explicit_source_exception(self):
        resource=source_resource(generation_kind=2)
        resource['video'].update(resolution='480p',width=854,height=480)
        result=self.execute(resource=resource)
        self.assertFalse(result['upstream']['ok'])
        self.assertIn('promote',result['upstream']['error']['message'])

    def test_explicit_sample_exception_does_not_become_sample_generation(self):
        resource=source_resource(generation_kind=2)
        resource['video'].update(resolution='480p',width=854,height=480)
        result=self.execute(payload(allowSampleSource=True),resource)
        self.assertTrue(result['upstream']['ok'])
        self.assertTrue(result['plan']['source']['sampleException'])
        self.assertEqual(result['plan']['generationKind'],'ordinary')
        self.assertFalse(result['generationSubmitted'])

    def test_non_sample_low_resolution_is_rejected(self):
        resource=source_resource(generation_kind=1)
        resource['video'].update(resolution='480p',width=854,height=480)
        self.assertFalse(self.execute(resource=resource)['upstream']['ok'])

    def test_missing_source_vid_or_unsuccessful_source_is_rejected(self):
        a=source_resource();a['video'].pop('vid')
        b=source_resource(status=200)
        for resource in (a,b,source_resource(generation_kind=99)):
            with self.subTest(resource=resource):self.assertFalse(self.execute(resource=resource)['upstream']['ok'])

    def test_target_must_be_empty_and_linked_only_to_exact_source(self):
        cases=[]
        a=target_node();a['status']='running';cases.append(a)
        a=target_node();a['resources']=[{'resourceId':RESOURCE}];cases.append(a)
        a=target_node();a['upstreamNodeIds'].append('node_0123456789');cases.append(a)
        a=target_node();a['executionDependencyNodeIds']=[];cases.append(a)
        for target in cases:
            with self.subTest(target=target):self.assertFalse(self.execute(target=target)['upstream']['ok'])

    def test_live_attempt_rejects_prompt_not_yet_saved(self):
        result=self.execute(payload(dryRun=False,allowOrdinaryModel=True,expectedDraftVersion=11),
                            snapshot=draft_snapshot(prompt='different saved prompt'))
        self.assertEqual(result['upstream']['error']['code'],'EXTEND_DRAFT_PROMPT_MISMATCH')
        self.assertFalse(result['generationSubmitted'])
        self.assertFalse(result['workspaceMutationAttempted'])

    def test_live_attempt_requires_ordinary_acknowledgement_and_write_policy(self):
        with self.assertRaises(ValueError):extension.validate(payload(dryRun=False))
        with self.assertRaises(ValueError):extension.validate(payload(dryRun=False,allowOrdinaryModel=True))
        with patch.dict(os.environ,{'JIMENG_READ_ONLY':'1'}),self.assertRaises(ValueError):
            extension.validate(payload(dryRun=False,allowOrdinaryModel=True,expectedDraftVersion=11))
        with patch.dict(os.environ,{'JIMENG_MAX_CREDITS':'1000'}),self.assertRaises(ValueError):
            extension.validate(payload(dryRun=False,allowOrdinaryModel=True,expectedDraftVersion=11))

    def test_callers_cannot_supply_provider_identity_headers_urls_or_unenforceable_ceiling(self):
        for key,value in [('vid','input'),('url','https://example.com'),('headers',{}),('cookie','x'),('creditCeiling',1),('benefitType','x')]:
            with self.subTest(key=key),self.assertRaises(ValueError):extension.validate(payload(**{key:value}))

    def test_only_observed_model_duration_and_plain_prompt_are_accepted(self):
        for changes in [{'model':'seedance_2.5_draft'},{'durationSeconds':10},{'durationSeconds':True},
                        {'prompt':'{{vid:untrusted}}'},{'targetNodeId':SOURCE},{'projectId':PROJECT.upper()}]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):extension.validate(payload(**changes))

    def test_recovery_reuses_original_identity_without_inventing_a_draft_save(self):
        a=extension.recovery(payload(),'original-job');b=extension.recovery(payload(),'original-job')
        self.assertEqual(a,b)
        self.assertEqual(a['targetNodeId'],TARGET)
        self.assertNotEqual(a['resourceId'],extension.recovery(payload(),'another-job')['resourceId'])
        self.assertTrue(a['doNotResubmit']);self.assertFalse(a['draftSaveAttempted'])
        self.assertNotIn('updateId',a)

    def test_body_uses_real_mode_nine_and_authoritative_source_ids(self):
        source={'vid':'internal-source-video-id','aspectRatio':'16:9','resolution':'1080p'}
        ids=extension._identities(payload(),'original')
        body=extension._observed_body(payload(),ids,source,{'benefit_type':'seedance_25_1080p_output'})
        item=body['initial']['items'][0];params=item['params']['video_params']
        self.assertEqual(params['mode'],9);self.assertEqual(params['model_name'],'seedance_2.5')
        self.assertEqual(params['materials'],[{'type':3,'vid':source['vid'],'role':4}])
        self.assertEqual(item['reference_sources'],[{'material_idx':0,'node_id':SOURCE,'resource_id':SOURCE_RESOURCE}])
        self.assertEqual(params['duration_ms'],5000)

    def test_commerce_is_read_from_fixed_current_specs_without_extrapolation(self):
        data={'commerce_config':{'model_config':{'seedance_2.5':{'video_resolution_price_configs':{'1080p':{
            'price':{'resource_type':'aigc','resource_id':'generate_video','benefit_type':'seedance_25_1080p_output'}}}}}}}
        with patch.object(extension,'_request',return_value=data) as call:
            self.assertEqual(extension._commerce('1080p',SESSION),{'benefit_type':'seedance_25_1080p_output'})
            call.assert_called_once_with(extension.SPECS_PATH,None,SESSION)
        data['commerce_config']['model_config']['seedance_2.5']['video_resolution_price_configs']['1080p']['price']['benefit_type']='unverified-benefit'
        with patch.object(extension,'_request',return_value=data),self.assertRaises(ValueError):extension._commerce('1080p',SESSION)

    def test_existing_status_verifies_mode_lineage_and_original_submission(self):
        target=target_node();target.update(status='success',resources=[{'resourceId':RESOURCE}])
        p={'action':'video.extend.status','projectId':PROJECT,'targetNodeId':TARGET,'resourceId':RESOURCE,'submitId':SUBMIT,
           'sourceNodeId':SOURCE,'sourceResourceId':SOURCE_RESOURCE}
        with patch.object(extension,'_node',return_value=target),patch.object(extension,'_resource',return_value=existing_extension()):
            result=extension.execute(p,'read-job',invoke=lambda args:None)
        data=result['upstream']['data'];self.assertEqual(data['state'],'succeeded')
        self.assertEqual(data['generationKind'],'ordinary');self.assertEqual(data['extensionSeconds'],5)
        self.assertEqual(data['video']['durationSeconds'],9.079);self.assertFalse(result['generationSubmitted'])

    def test_status_rejects_mismatched_submit_or_non_extension(self):
        target=target_node();target.update(status='success',resources=[{'resourceId':RESOURCE}])
        p={'action':'video.extend.status','projectId':PROJECT,'targetNodeId':TARGET,'resourceId':RESOURCE,'submitId':SUBMIT}
        a=existing_extension();a['submit_id']=PROJECT
        b=existing_extension();b['video']['generation_mode']=1
        c=existing_extension();c['generation_kind']=2
        for resource in (a,b,c):
            with patch.object(extension,'_node',return_value=target),patch.object(extension,'_resource',return_value=resource):
                self.assertFalse(extension.execute(p,'read-job',invoke=lambda args:None)['upstream']['ok'])

    def test_resource_read_is_fixed_and_drops_provider_media_urls(self):
        envelope={'ret':'0','data':{'resources':[source_resource()],'media_urls':{'secret':'https://signed.example/'}}}
        with patch.object(extension.web_actions,'_send',return_value=(200,json.dumps(envelope).encode())) as send:
            resource=extension._resource(PROJECT,SOURCE_RESOURCE,SESSION)
        self.assertEqual(resource['resource_id'],SOURCE_RESOURCE)
        self.assertEqual(send.call_args.args[0],extension.RESOURCE_PATH)
        self.assertEqual(send.call_args.args[1],{'project_id':PROJECT,'resource_ids':[SOURCE_RESOURCE]})
        self.assertNotIn('media_urls',resource)

    def test_transport_errors_do_not_echo_private_session_or_retry(self):
        with patch.object(extension,'_node',return_value=source_node()), \
             patch.object(extension.web_actions,'_send',side_effect=ValueError(SESSION['cookie'])) as send:
            result=extension.execute(payload(),'job',invoke=lambda args:None)
        self.assertEqual(send.call_count,1)
        self.assertNotIn(SESSION['cookie'],json.dumps(result))
        self.assertEqual(result['upstream']['error']['code'],'EXTEND_READ_UNAVAILABLE')

    def test_capabilities_limit_live_extension_to_prepared_target_and_do_not_claim_acceptance(self):
        caps=extension.capabilities()['actions']
        self.assertTrue(caps['video.extend']['liveExecutionAllowed'])
        self.assertFalse(caps['video.extend']['serverExecutionVerified'])
        self.assertTrue(caps['video.extend.status']['liveExecutionAllowed'])
        self.assertFalse(caps['video.extend']['targetAutoCreationImplemented'])

    def test_raw_draft_requires_matching_head_version_profile_and_json_version(self):
        snapshot=draft_snapshot()
        data={'project':{'project_id':PROJECT},'draft_version':11,'head_draft_version':11,
              'data_version':extension.DRAFT_DATA_VERSION,'draft_schema_profile':extension.DRAFT_SCHEMA_PROFILE,
              'draft_json':json.dumps(snapshot['document'])}
        with patch.object(extension,'_request',return_value=data) as read:
            self.assertEqual(extension._draft(PROJECT,SESSION),snapshot)
            read.assert_called_once_with(extension.DRAFT_PATH,{'project_id':PROJECT,'need_draft_json':True},SESSION)
        for key,value in [('head_draft_version',12),('data_version','unsupported'),('draft_schema_profile','unsupported'),
                          ('draft_json',json.dumps({**snapshot['document'],'draftVersion':10}))]:
            with self.subTest(key=key),patch.object(extension,'_request',return_value={**data,key:value}),self.assertRaises(ValueError):
                extension._draft(PROJECT,SESSION)

    def test_raw_draft_rejects_wrong_settings_slot_current_source_and_extra_edge(self):
        cases=[]
        s=draft_snapshot();s['document']['nodes'][1]['data']['generationDraft']['contentSettings']['mode']='multiModal';cases.append(s)
        s=draft_snapshot();s['document']['nodes'][1]['data']['generationDraft']['references'][0]['slotId']=RESOURCE;cases.append(s)
        s=draft_snapshot();s['document']['nodes'][0]['data']['resourceId']=RESOURCE;cases.append(s)
        s=draft_snapshot();s['document']['edges'].append({'id':'edge_9876543210','source':SOURCE,'target':TARGET,'type':'reference'});cases.append(s)
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):self.assertFalse(self.execute(snapshot=snapshot)['upstream']['ok'])
        self.assertFalse(self.execute(payload(expectedDraftVersion=10))['upstream']['ok'])

    def test_live_reads_again_then_sends_once_preserving_saved_part_order(self):
        p=payload(dryRun=False,allowOrdinaryModel=True,expectedDraftVersion=11)
        with patch.object(extension,'_node',side_effect=[source_node(),target_node(),target_node()]), \
             patch.object(extension,'_resource',return_value=source_resource()), \
             patch.object(extension,'_commerce',return_value={'benefit_type':'seedance_25_1080p_output'}), \
             patch.object(extension,'_draft',return_value=draft_snapshot()) as read, \
             patch.object(extension,'_submit_once',return_value=True) as send:
            result=extension.execute(p,'test-job',invoke=lambda args:None)
        self.assertEqual(read.call_count,2);send.assert_called_once()
        params=send.call_args.args[0]['initial']['items'][0]['params']['video_params']
        self.assertEqual(params['prompt'],'{{video:0}}'+p['prompt'])
        self.assertEqual(params['meta_list'][-1],{'type':1,'text':p['prompt']})
        self.assertTrue(result['generationSubmitted']);self.assertFalse(result['upstream']['data']['generationComplete'])

    def test_live_changed_head_never_submits(self):
        p=payload(dryRun=False,allowOrdinaryModel=True,expectedDraftVersion=11)
        with patch.object(extension,'_node',side_effect=[source_node(),target_node(),target_node()]), \
             patch.object(extension,'_resource',return_value=source_resource()), \
             patch.object(extension,'_commerce',return_value={'benefit_type':'seedance_25_1080p_output'}), \
             patch.object(extension,'_draft',side_effect=[draft_snapshot(),draft_snapshot(version=12)]), \
             patch.object(extension,'_submit_once') as send:
            result=extension.execute(p,'test-job',invoke=lambda args:None)
        send.assert_not_called();self.assertEqual(result['upstream']['error']['code'],'EXTEND_DRAFT_CHANGED')

    def test_uncertain_single_submission_raises_for_unknown_job_without_retry_or_secret(self):
        source={'vid':'internal-source-video-id','aspectRatio':'16:9','resolution':'1080p'}
        body=extension._observed_body(payload(),extension._identities(payload(),'job'),source,{'benefit_type':'x'})
        for response in [None,(200,b'{'),(500,b'{}'),(200,b'{"ret":"0","data":{"results":[]}}')]:
            with self.subTest(response=response),patch.object(extension.web_actions,'_send',
                    side_effect=RuntimeError(SESSION['cookie']) if response is None else None,return_value=response) as send:
                with self.assertRaises(RuntimeError) as error:extension._submit_once(body,SESSION)
                send.assert_called_once();self.assertNotIn(SESSION['cookie'],str(error.exception))

    def test_submit_requires_exact_acknowledged_resource_and_maps_explicit_rejection(self):
        source={'vid':'internal-source-video-id','aspectRatio':'16:9','resolution':'1080p'}
        body=extension._observed_body(payload(),extension._identities(payload(),'job'),source,{'benefit_type':'x'})
        rid=body['initial']['items'][0]['resource_id']
        good={'ret':'0','data':{'results':[{'resource_id':rid,'error_code':0,'state':1}]}}
        for doc,accepted in [(good,True),({'ret':'5'},False)]:
            with patch.object(extension.web_actions,'_send',return_value=(200,json.dumps(doc).encode())):
                self.assertEqual(extension._submit_once(body,SESSION),accepted)
        good['data']['results'][0]['resource_id']=RESOURCE
        with patch.object(extension.web_actions,'_send',return_value=(200,json.dumps(good).encode())),self.assertRaises(RuntimeError):
            extension._submit_once(body,SESSION)


if __name__=='__main__':unittest.main()
