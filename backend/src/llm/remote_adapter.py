"""Explicit ordinary-NPC DeepSeek route. No original modules patched; no canonical identity."""
import json,os
from threading import Lock
from copy import deepcopy
import httpx
from openai import OpenAI,AsyncOpenAI
from src.llm.client_base import ClientBase,VisionMode
from src.http.routes.mantella_route import mantella_route
from src.http.legacy_package_protocol import LegacyPackageProtocol,PACKAGE_PROTOCOL
from src.games.fallout4 import Fallout4
from src.tts.tts_factory import create_tts
from src.output_manager import ChatManager
from src.game_manager import GameStateManager
from src.llm.openclaw.router import OpenClawRouter
from src.llm.openclaw.config import OpenClawCompanionConfig

ENDPOINT='https://api.deepseek.com'
MODEL='deepseek-flash'
PARAMS={'max_tokens':128,'temperature':0.4,'extra_body':{'thinking':{'type':'disabled'}}}

def runtime_key():
    """Resolve only the explicit environment variable at request time; no file fallback."""
    value = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    if not value:
        raise ValueError('Set DEEPSEEK_API_KEY before an explicitly authorized remote request')
    return value

def validate(config):
    for flag in ('vision_enabled','use_game_screenshots','advanced_actions_enabled','random_llm_enabled',
                 'allow_per_character_llm_overrides','allow_per_character_tts_overrides',
                 'apply_model_profiles','allow_interruption','enable_telemetry'):
        if getattr(config,flag,None) is not False:raise ValueError('Disabled capability required: '+flag)
    if config.conversation_summary_enabled is not True or config.summary_llm_enabled is not True:
        raise ValueError('Explicit separate summaries required')
    for prefix in ('','summary_'):
        if getattr(config,prefix+'llm_api')!=ENDPOINT or getattr(config,prefix+'llm')!=MODEL:
            raise ValueError('Exact DeepSeek dialogue AND summary route required')
        if getattr(config,prefix+'llm_params')!=PARAMS:raise ValueError('Exact bounded non-thinking params required')
    if str(config.game.name)!='FALLOUT4' or int(config.port)!=4999:raise ValueError('Fallout loopback callback port required')
    LegacyPackageProtocol(PACKAGE_PROTOCOL).check_config(config)

def messages_for(value):
    if hasattr(value,'get_openai_messages'):messages=value.get_openai_messages()
    elif hasattr(value,'get_openai_message'):messages=[value.get_openai_message()]
    else:raise ValueError('Expected actual Mantella message/thread')
    validate_messages(messages)
    return messages

def validate_messages(messages):
    if not isinstance(messages,list) or not messages:raise ValueError('Explicit text messages required')
    for message in messages:
        if not isinstance(message,dict) or set(message)-{'role','content'} or message.get('role') not in ('system','user','assistant') or not isinstance(message.get('content'),str):
            raise ValueError('Only ordinary text messages accepted')
    if len(json.dumps(messages,ensure_ascii=False).encode())>65536:raise ValueError('Bounded input exceeded')

class RemoteClient(ClientBase):
    def __init__(self,config,summary=False,*,key_resolver=runtime_key,sync_transport=None,async_transport=None):
        # Deliberately avoid ClientBase startup: no key-file discovery, model profiles,
        # auxiliary provider allocation, tokenizer network or eager async client.
        validate(config)
        self._model_name=MODEL;self._base_url=ENDPOINT;self._request_params=deepcopy(PARAMS)
        self._token_limit=config.summary_custom_token_count if summary else config.custom_token_count
        self._is_local=False;self._caching_enabled=False;self._startup_async_client=None
        self._image_client=self._function_client=None;self._enable_vision_next_call=False
        self._vision_mode=VisionMode.DISABLED
        self._key_resolver=key_resolver;self._sync_transport=sync_transport;self._async_transport=async_transport
        self._failed=False;self._call_lock=Lock()
    def get_count_tokens(self,messages):
        if hasattr(messages,'get_openai_messages'):messages=messages.get_openai_messages()
        elif hasattr(messages,'get_openai_message'):messages=messages.get_openai_message()
        return len(str(messages).encode('utf-8'))
    def is_too_long(self,messages,token_limit_percent):return self.get_count_tokens(messages)>self._token_limit*token_limit_percent
    def _check_request(self,request):
        if str(request.url)!=ENDPOINT+'/chat/completions' or request.method!='POST':raise ValueError('Unauthorized API origin/path')
        body=json.loads(request.content)
        if set(body)-{'model','messages','stream','max_tokens','temperature','thinking'}:raise ValueError('Unsupported API body field')
        if body.get('model')!=MODEL or body.get('max_tokens')!=128 or body.get('temperature')!=0.4 or body.get('thinking')!={'type':'disabled'}:
            raise ValueError('API request route/parameters changed')
        if 'stream' in body and type(body['stream']) is not bool:raise ValueError('Invalid stream flag')
        validate_messages(body.get('messages'))
    async def _check_async_request(self,request):self._check_request(request)
    def generate_sync_client(self):
        return OpenAI(api_key=self._key_resolver(),base_url=ENDPOINT,organization='',project='',webhook_secret='',max_retries=0,timeout=45,
                      http_client=httpx.Client(trust_env=False,follow_redirects=False,timeout=45,transport=self._sync_transport,event_hooks={'request':[self._check_request]}))
    def generate_async_client(self):
        return AsyncOpenAI(api_key=self._key_resolver(),base_url=ENDPOINT,organization='',project='',webhook_secret='',max_retries=0,timeout=45,
                           http_client=httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=45,transport=self._async_transport,event_hooks={'request':[self._check_async_request]}))
    def request_call(self,messages):
        data=messages_for(messages)
        if self._failed or not self._call_lock.acquire(blocking=False):raise RuntimeError('Remote client stopped/busy; no retry')
        try:
            with self.generate_sync_client() as client:
                response=client.chat.completions.create(model=MODEL,messages=data,**deepcopy(PARAMS))
                return response.choices[0].message.content if response.choices else None
        except Exception:
            self._failed=True
            raise RuntimeError('Remote summary failed; client latched closed, review failure before explicit restart') from None
        finally:self._call_lock.release()
    async def streaming_call(self,messages,is_multi_npc,tools=None):
        if tools:raise ValueError('Tool calls excluded from bounded remote route')
        data=messages_for(messages)
        if self._failed or not self._call_lock.acquire(blocking=False):raise RuntimeError('Remote client stopped/busy; no retry')
        client=None;stream=None
        try:
            client=self.generate_async_client()
            stream=await client.chat.completions.create(model=MODEL,messages=data,stream=True,**deepcopy(PARAMS))
            async for chunk in stream:
                if chunk.choices:
                    delta=chunk.choices[0].delta
                    if delta.tool_calls:raise ValueError('Unexpected tool response')
                    if delta.content:yield ('content',delta.content)
        except Exception:
            self._failed=True
            raise RuntimeError('Remote dialogue failed; client latched closed, review failure before explicit restart') from None
        except BaseException:
            # Cancellation/early close can follow remote acceptance: never replay implicitly.
            self._failed=True
            raise
        finally:
            try:
                if stream is not None:await stream.close()
            finally:
                try:
                    if client is not None:await client.close()
                finally:self._call_lock.release()

class RemoteDialogueClient(RemoteClient):
    def __init__(self,config,**kwargs):super().__init__(config,False,**kwargs)
class RemoteSummaryClient(RemoteClient):
    def __init__(self,config,**kwargs):super().__init__(config,True,**kwargs)

class RemoteMantellaRoute(mantella_route):
    """Override only construction; inherited installed legacy callback/lifecycle unchanged."""
    def _setup_route(self):
        config=self._config;validate(config)
        # Exact installed superclass uses these private state slots in dispatch.
        protocol=self._mantella_route__package_protocol
        if protocol is None:raise ValueError('Legacy package protocol required')
        protocol.check_config(config);protocol.reset()
        prior=self._mantella_route__game
        if prior:prior.end_conversation({})
        game=Fallout4(config);tts=create_tts(config.tts_service,config,game)
        dialogue=RemoteDialogueClient(config);summary=RemoteSummaryClient(config)
        disabled=OpenClawRouter(OpenClawCompanionConfig(enabled=False,fallback_to_direct=False))
        chat=ChatManager(config,tts,dialogue,game,openclaw_router=disabled)
        self._mantella_route__game=GameStateManager(game,chat,config,self._mantella_route__language_info,dialogue,summary)
