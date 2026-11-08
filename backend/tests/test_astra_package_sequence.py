import asyncio,time
from tests.test_paired_candidate_interactions import package_goodbye,post,CONTINUE,GOODBYE_S,chat_manager_factory
from src.http.communication_constants import communication_constants as C
def test_actual_package_end_action_waits_before_cleanup_ack(package_goodbye):
 s=package_goodbye
 async def scenario():
  goodbye,issued=await post(s.client,dict(CONTINUE))
  assert goodbye[C.KEY_REPLYTYPE_NPCTALK][C.KEY_ACTOR_ACTIONS]==[C.ACTION_ENDCONVERSATION]
  end,done=await post(s.client,{C.KEY_REQUESTTYPE:C.KEY_REQUESTTYPE_ENDCONVERSATION})
  await s.client.aclose()
  assert end[C.KEY_REPLYTYPE]==C.KEY_REPLYTYPE_ENDCONVERSATION
  assert done-issued >= GOODBYE_S-0.05, ('package EndConversation acknowledgement bypassed goodbye hold',done-issued)
 asyncio.run(scenario())