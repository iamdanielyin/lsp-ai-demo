"""Isolated browser fixture, never a tenant acceptance result. Binds loopback only."""
import itertools
import json
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from lsp.app import create_app
from lsp.service import normalize_message
from waitress import serve

if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='lsp_browser_test_') as directory:
        app=create_app({'DATABASE_PATH':directory+'/test.sqlite3','FILE_DIRECTORY':directory+'/files',
                        'CONFIG_MASTER_KEY':Fernet.generate_key().decode(),'ADMIN_INITIAL_PASSWORD':'LOCAL-BROWSER-TEST-ONLY'},start_worker=False)
        svc=app.extensions['service']
        svc.settings.save({'platform_api_base_url':'https://browser-test.freshchat.com','freshchat_token':'LOCAL-TEST-NOT-A-CREDENTIAL',
                           'reply_actor_id':'LOCAL_TEST_AGENT','source_mapping':{'web':'Webchat'},'allowed_channels':['Webchat'],
                           'test_identity_allowlist':['user:LOCAL_TEST_CUSTOMER'],'requester_mapping':{'LOCAL_TEST_CUSTOMER':42},
                           'freshdesk_domain':'https://browser-test.freshdesk.com','freshdesk_api_key':'LOCAL-TEST-NOT-A-CREDENTIAL',
                           'public_base_url':'https://local-test.example','openai_model':'LOCAL_TEST_STUB','openai_api_key':'LOCAL-TEST-NOT-A-CREDENTIAL',
                           'knowledge_text':'本服务器仅用于本地 UI 测试。所有会话、模型响应和工单为合成数据，不能用作真实验收。'})
        c=svc.add_conversation('LOCAL_TEST_CONVERSATION','LOCAL_TEST_CUSTOMER','web')
        rows=[]
        for i in range(123):
            created=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=i)).isoformat()
            row={'id':f'LOCAL_TEST_MESSAGE_{i:03d}','conversation_id':'LOCAL_TEST_CONVERSATION','user_id':'LOCAL_TEST_CUSTOMER',
                 'actor_type':'user' if i%3 else 'agent','actor_id':'LOCAL_TEST_CUSTOMER' if i%3 else 'LOCAL_TEST_HUMAN',
                 'message_type':'normal','message_source':'web','created_time':created,
                 'message_parts':[{'text':{'content':f'本地 UI 合成测试消息 {i+1}。这不是客户真实会话。'}}]}
            if i==122:row['message_parts']=[{'text':{'content':'<img src=x onerror="window.__xss=true"> 请说明测试停车方案，再附 PDF。'}}]
            rows.append(row);svc.insert_message(c,normalize_message(row,c['platform_id']))
        svc.db.run('UPDATE conversations SET sync_complete=1 WHERE id=?',(c['id'],))
        asset=svc.save_asset({'asset_id':'LOCAL_TEST_PDF','name':'本地测试 PDF','purpose':'仅用于 UI 测试，不是真实方案','channels':['Webchat']},b'%PDF-1.4\n% Local browser test only\n%%EOF\n','LOCAL_TEST.pdf','application/pdf')
        svc.db.run("UPDATE assets SET state='sendable',ref=? WHERE id=?",(svc.db.seal({'file_hash':'LOCAL_TEST_HASH'}),asset['id']))
        counter=itertools.count(1)
        def send(settings,cid,message,asset=None):
            mid='LOCAL_TEST_OUTBOUND_'+str(next(counter))
            row={'id':mid,'conversation_id':cid,'user_id':'LOCAL_TEST_CUSTOMER','actor_type':'agent','actor_id':'LOCAL_TEST_AGENT',
                 'message_type':'normal','created_time':datetime.now(timezone.utc).isoformat(),'message_parts':[{'text':{'content':message.get('text') or '[本地媒体发送测试]'}}]}
            rows.append(row)
            svc.ingest({'action':'message_create','data':{'message':row}},'LOCAL_TEST','0')
            return {'id':mid}
        def model(settings,payload):
            plan={'messages':[{'type':'text','text':'这是本地 UI 测试模型替身的回复，不代表 OpenAI 已验收。','asset_id':None}], 'needs_human':False,'ticket_reason':'本地测试跟进'}
            return {'id':'LOCAL_TEST_RESPONSE','status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(plan)}]}],'usage':{'input_tokens':100,'output_tokens':30,'total_tokens':130}},'LOCAL_TEST_REQUEST'
        with patch('lsp.providers.agents',return_value=[{'id':'LOCAL_TEST_AGENT','name':'本地测试坐席'}]),patch('lsp.providers.conversation',return_value={'conversation_id':c['platform_id']}),patch('lsp.providers.history_pages',side_effect=lambda *args:iter([rows.copy()])),patch('lsp.providers.upload',side_effect=lambda s,a,data: ({'url':'https://local-test.example/image.png'} if a['kind']=='image' else {'file_hash':'LOCAL_TEST_HASH','file_security_status':'SAFE_FILE'},'sendable')),patch('lsp.providers.send_message',side_effect=send),patch('lsp.providers.openai',side_effect=model),patch('lsp.providers.freshdesk',return_value={'id':999,'status':2}):
            svc.start()
            print('LOCAL SYNTHETIC UI TEST ONLY http://127.0.0.1:8128 — password: LOCAL-BROWSER-TEST-ONLY',flush=True)
            serve(app,host='127.0.0.1',port=8128,threads=4)
