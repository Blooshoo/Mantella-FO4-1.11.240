"""Offline only: fake HTTP transports, actual SDKs/classes, no credentials or inference."""
from pathlib import Path
import asyncio,copy,json,os,sys,unittest
from unittest.mock import patch
from types import SimpleNamespace as NS
import httpx
from src.llm.messages import SystemMessage
from src.http.legacy_package_protocol import PACKAGE_PROTOCOL
from src.llm import remote_adapter as adapter


def config():
    c=NS(game=NS(name='FALLOUT4'),narration_handling=NS(name='CUT_NARRATIONS'),actions=[],
         tts_service='Piper',narration_indicators=None,port=4999,conversation_summary_enabled=True,summary_llm_enabled=True,
         custom_token_count=4096,summary_custom_token_count=4096)
    for flag in ('vision_enabled','use_game_screenshots','advanced_actions_enabled','random_llm_enabled',
                 'allow_per_character_llm_overrides','allow_per_character_tts_overrides',
                 'apply_model_profiles','allow_interruption','enable_telemetry'):
        setattr(c,flag,False)
    for prefix in ('','summary_'):
        setattr(c,prefix+'llm_api',adapter.ENDPOINT)
        setattr(c,prefix+'llm',adapter.MODEL)
        setattr(c,prefix+'llm_params',copy.deepcopy(adapter.PARAMS))
    return c

def completion():return {'id':'offline','object':'chat.completion','created':1,'model':adapter.MODEL,'choices':[{'index':0,'message':{'role':'assistant','content':'OFFLINE summary only.'},'finish_reason':'stop'}]}

def event():return ('data: '+json.dumps({'id':'offline','object':'chat.completion.chunk','created':1,'model':adapter.MODEL,'choices':[{'index':0,'delta':{'content':'OFFLINE greeting.'},'finish_reason':None}]})+'\n\n').encode()

class Track:
    def generate_sync_client(self):
        c=super().generate_sync_client();self.seen.append(c);return c
    def generate_async_client(self):
        c=super().generate_async_client();self.seen.append(c);return c
class Dialogue(Track,adapter.RemoteDialogueClient):
    def __init__(self,*a,**kw):self.seen=[];super().__init__(*a,**kw)
class Summary(Track,adapter.RemoteSummaryClient):
    def __init__(self,*a,**kw):self.seen=[];super().__init__(*a,**kw)

class GateTests(unittest.TestCase):
    def test_explicit_config_accepted(self):adapter.validate(config())
    def test_remote_factories_no_eager_key(self):
        with patch.object(adapter,'runtime_key',side_effect=AssertionError('No credential reads')):
            c=Dialogue(config(),key_resolver=lambda: (_ for _ in ()).throw(AssertionError('eager credential')))
            self.assertEqual(c.seen,[]);self.assertFalse(c.is_local);self.assertIsNone(c._startup_async_client)
    def test_both_urls_and_models_refuse_changes(self):
        for field,value in [('llm_api','http://127.0.0.1:11434/v1'),('summary_llm_api','https://api.openai.com/v1'),('llm','default'),('summary_llm','deepseek-v4-pro')]:
            with self.subTest(field=field):
                c=config();setattr(c,field,value)
                with self.assertRaises(ValueError):Dialogue(c)
    def test_auxiliary_flags_refused(self):
        for flag in ('vision_enabled','use_game_screenshots','advanced_actions_enabled','random_llm_enabled','allow_per_character_llm_overrides','allow_per_character_tts_overrides','apply_model_profiles','allow_interruption','enable_telemetry'):
            with self.subTest(flag=flag):
                c=config();setattr(c,flag,True)
                with self.assertRaises(ValueError):Summary(c)
    def test_parameter_injection_refused(self):
        for field in ('llm_params','summary_llm_params'):
            c=config();p=copy.deepcopy(adapter.PARAMS);p['extra_body']['model']='other';setattr(c,field,p)
            with self.assertRaises(ValueError):Dialogue(c)
    def test_route_scoped_factory_no_original_patching(self):
        c=config();seen={};original=__import__('src.http.routes.mantella_route',fromlist=['LLMClient']).LLMClient
        def chat(cfg,tts,dialogue,game,**kw):seen.update(dialogue=dialogue,router=kw['openclaw_router']);return object()
        def manager(game,chat,cfg,lang,dialogue,summary):seen['summary']=summary;return object()
        with patch.multiple(adapter,Fallout4=lambda c:object(),create_tts=lambda *a:object(),ChatManager=chat,GameStateManager=manager):
            route=adapter.RemoteMantellaRoute(c,{},package_protocol=PACKAGE_PROTOCOL);route._setup_route()
        self.assertIsInstance(seen['dialogue'],adapter.RemoteDialogueClient);self.assertIsInstance(seen['summary'],adapter.RemoteSummaryClient)
        self.assertFalse(seen['router'].enabled);self.assertFalse(seen['router'].cfg.fallback_to_direct)
        self.assertIs(__import__('src.http.routes.mantella_route',fromlist=['LLMClient']).LLMClient,original)
    def test_exact_request_hook_rejects_origin_path(self):
        c=Dialogue(config(),key_resolver=lambda:'OFFLINE')
        for url in ('https://evil.invalid/chat/completions','https://api.deepseek.com/other','https://api.deepseek.com/chat/completions?x=1'):
            with self.assertRaises(ValueError):c._check_request(httpx.Request('POST',url,json={}))
    def test_summary_real_sdk_closure(self):
        responses=[];requests=[]
        def handler(r):
            requests.append(r);response=httpx.Response(200,json=completion());responses.append(response);return response
        c=Summary(config(),key_resolver=lambda:'OFFLINE-NOT-A-CREDENTIAL',sync_transport=httpx.MockTransport(handler))
        self.assertEqual(c.request_call(SystemMessage('OFFLINE synthetic conversation',config())),'OFFLINE summary only.')
        self.assertEqual(len(requests),1);self.assertTrue(all(r.is_closed for r in responses));self.assertTrue(c.seen[0].is_closed())
        data=json.loads(requests[0].content);self.assertEqual(data['model'],adapter.MODEL);self.assertEqual(data['thinking'],{'type':'disabled'})
        self.assertEqual(str(requests[0].url),adapter.ENDPOINT+'/chat/completions')
    def test_summary_auth_no_retry_or_secret_in_error(self):
        requests=[]
        def handler(r):requests.append(r);return httpx.Response(401,json={'error':{'message':'OFFLINE refusal'}})
        c=Summary(config(),key_resolver=lambda:'OFFLINE-NOT-A-CREDENTIAL',sync_transport=httpx.MockTransport(handler));m=SystemMessage('OFFLINE',config())
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError,'closed|stopped'):c.request_call(m)
        self.assertEqual(len(requests),1);self.assertTrue(c.seen[0].is_closed())
    def test_environment_not_inherited(self):
        def handler(request):
            self.assertNotIn('evil',str(request.url));self.assertFalse(request.headers.get('openai-organization'));return httpx.Response(200,json=completion())
        with patch.dict(os.environ,{'OPENAI_BASE_URL':'https://evil.invalid','OPENAI_ORG_ID':'evil','OPENAI_PROJECT_ID':'evil','OPENAI_WEBHOOK_SECRET':'evil','HTTPS_PROXY':'http://127.0.0.1:9','SSL_CERT_FILE':'/absent'}):
            c=Summary(config(),key_resolver=lambda:'OFFLINE',sync_transport=httpx.MockTransport(handler));c.request_call(SystemMessage('OFFLINE',config()))

class Bytes(httpx.AsyncByteStream):
    def __init__(self,wait=False):self.closed=False;self.wait=wait;self.waiting=asyncio.Event()
    async def __aiter__(self):
        yield event()
        if self.wait:
            self.waiting.set();await asyncio.Event().wait()
        yield b'data: [DONE]\n\n'
    async def aclose(self):self.closed=True

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    def make(self,wait=False,status=200):
        stream=Bytes(wait);responses=[];requests=[]
        async def handler(r):
            requests.append(r)
            response=httpx.Response(status,stream=stream,headers={'content-type':'text/event-stream','location':'https://evil.invalid/'}) if status==200 else httpx.Response(status,json={'error':{'message':'OFFLINE'}},headers={'location':'https://evil.invalid/'})
            responses.append(response);return response
        client=Dialogue(config(),key_resolver=lambda:'OFFLINE-NOT-A-CREDENTIAL',async_transport=httpx.MockTransport(handler))
        return client,stream,responses,requests
    async def test_dialogue_sdk_completion(self):
        c,s,res,req=self.make();items=[x async for x in c.streaming_call(SystemMessage('OFFLINE',config()),False)]
        self.assertEqual(items,[('content','OFFLINE greeting.')]);self.assertTrue(s.closed);self.assertTrue(res[0].is_closed);self.assertTrue(c.seen[0].is_closed())
    async def test_early_close_closes_actual_response(self):
        c,s,res,req=self.make(True);g=c.streaming_call(SystemMessage('OFFLINE',config()),False);await anext(g);await g.aclose()
        self.assertTrue(s.closed);self.assertTrue(res[0].is_closed);self.assertTrue(c.seen[0].is_closed());self.assertFalse(c._call_lock.locked());self.assertTrue(c._failed)
    async def test_cancel_pending_stream_closes_response(self):
        c,s,res,req=self.make(True)
        async def collect():return [x async for x in c.streaming_call(SystemMessage('OFFLINE',config()),False)]
        task=asyncio.create_task(collect());await asyncio.wait_for(s.waiting.wait(),2);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(s.closed);self.assertTrue(res[0].is_closed);self.assertTrue(c.seen[0].is_closed());self.assertFalse(c._call_lock.locked());self.assertTrue(c._failed)
    async def test_http_auth_and_redirect_no_retry(self):
        for status in (401,403,429,503,302):
            with self.subTest(status=status):
                c,s,res,req=self.make(status=status);m=SystemMessage('OFFLINE',config())
                for _ in range(2):
                    with self.assertRaises(RuntimeError):[x async for x in c.streaming_call(m,False)]
                self.assertEqual(len(req),1);self.assertTrue(res[0].is_closed);self.assertTrue(c.seen[0].is_closed())
    async def test_cancel_before_response_allocation(self):
        entered=asyncio.Event()
        async def handler(request):
            entered.set();await asyncio.Event().wait()
        c=Dialogue(config(),key_resolver=lambda:'OFFLINE',async_transport=httpx.MockTransport(handler))
        async def collect():return [x async for x in c.streaming_call(SystemMessage('OFFLINE',config()),False)]
        task=asyncio.create_task(collect());await asyncio.wait_for(entered.wait(),2);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(c.seen[0].is_closed());self.assertFalse(c._call_lock.locked());self.assertTrue(c._failed)
    async def test_tools_refused_before_factory(self):
        c,s,res,req=self.make()
        with self.assertRaises(ValueError):[x async for x in c.streaming_call(SystemMessage('OFFLINE',config()),False,[{'name':'not-allowed'}])]
        self.assertEqual(c.seen,[])

class RequestBodyTests(unittest.TestCase):
    def body(self):
        return {'model':adapter.MODEL,'messages':[{'role':'user','content':'OFFLINE'}],
                'max_tokens':128,'temperature':0.4,'thinking':{'type':'disabled'}}
    def test_valid_body_and_each_mutation(self):
        c=Dialogue(config(),key_resolver=lambda:'OFFLINE')
        body=self.body()
        c._check_request(httpx.Request('POST',adapter.ENDPOINT+'/chat/completions',json=body))
        for field,value in [('model','other'),('max_tokens',129),('temperature',1),
                            ('thinking',{'type':'enabled'}),('tools',[]),('stream','true'),
                            ('messages',[{'role':'tool','content':'OFFLINE'}]),
                            ('messages',[{'role':'user','content':[{'type':'image_url'}]}]),
                            ('messages',[{'role':'user','content':'X'*65537}])]:
            with self.subTest(field=field,value_type=type(value).__name__):
                changed=self.body();changed[field]=value
                with self.assertRaises(ValueError):
                    c._check_request(httpx.Request('POST',adapter.ENDPOINT+'/chat/completions',json=changed))
        with self.assertRaises(ValueError):
            c._check_request(httpx.Request('GET',adapter.ENDPOINT+'/chat/completions',json=body))
    def test_failure_latch_rejects_before_another_credential_resolution(self):
        calls=[]
        c=Summary(config(),key_resolver=lambda:calls.append(1) or 'OFFLINE',
                  sync_transport=httpx.MockTransport(lambda r:httpx.Response(503,json={'error':{'message':'OFFLINE'}})))
        for _ in range(2):
            with self.assertRaises(RuntimeError):c.request_call(SystemMessage('OFFLINE',config()))
        self.assertEqual(calls,[1])

class EnvironmentKeyTests(unittest.TestCase):
    def test_missing_environment_fails_without_file_reads(self):
        with patch.dict(os.environ,{},clear=True), patch('builtins.open',side_effect=AssertionError('file read')):
            with self.assertRaises(ValueError):adapter.runtime_key()
    def test_environment_key_resolved_only_when_called(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':' OFFLINE '}):
            self.assertEqual(adapter.runtime_key(),'OFFLINE')

if __name__=='__main__':unittest.main(verbosity=2)
