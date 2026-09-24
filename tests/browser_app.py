"""Isolated browser fixture, never a tenant acceptance result. Binds loopback only."""
import itertools
import binascii
import json
import struct
import sys
import tempfile
import time
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from lsp.app import create_app
from lsp import security
from lsp.service import normalize_message
from waitress import serve

if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='lsp_browser_test_') as directory:
        app=create_app({'DATABASE_PATH':directory+'/test.sqlite3','FILE_DIRECTORY':directory+'/files',
                        'CONFIG_MASTER_KEY':Fernet.generate_key().decode(),'ADMIN_INITIAL_PASSWORD':'LOCAL-BROWSER-TEST-ONLY'},start_worker=False)
        svc=app.extensions['service']
        svc.settings.save({'platform_api_base_url':'https://browser-test.freshchat.com','freshchat_token':'LOCAL-TEST-NOT-A-CREDENTIAL',
                           'reply_actor_id':'LOCAL_TEST_AGENT','source_mapping':{'web':'Webchat'},'allowed_channels':['Webchat'],
                           'test_identity_allowlist':['user:LOCAL_TEST_CUSTOMER'],
                           'freshdesk_domain':'https://browser-test.freshdesk.com','freshdesk_api_key':'LOCAL-TEST-NOT-A-CREDENTIAL',
                           'public_base_url':'https://local-test.example','openai_model':'LOCAL_TEST_STUB','openai_api_key':'LOCAL-TEST-NOT-A-CREDENTIAL',
                           'knowledge_text':'本服务器仅用于本地 UI 测试。所有会话、模型响应和工单为合成数据，不能用作真实验收。'})
        c=svc.add_conversation('LOCAL_TEST_CONVERSATION','LOCAL_TEST_CUSTOMER','web')
        def chunk(kind, data):
            return struct.pack('!I', len(data))+kind+data+struct.pack('!I', binascii.crc32(kind+data)&0xffffffff)
        pixels=b''.join(b'\0'+bytes((36, 99+y//8, 81))*960 for y in range(640))
        png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',960,640,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')
        mp4=(Path(__file__).parent/'fixtures'/'local-video.mp4').read_bytes()
        media_host='fc-use1-00-files-bkt-00.s3.amazonaws.com'
        media_files={'map.png':(png, 'image/png'),'guide.mp4':(mp4, 'video/mp4'),
                     'guide.pdf':(b'%PDF-1.4\n% LOCAL TEST\n%%EOF', 'application/pdf'),
                     'faq.md':(b'# Local synthetic FAQ', 'text/markdown')}
        def media_request(url, **kwargs):
            if '--slow-media' in sys.argv:time.sleep(5)
            security.url_parts(url, kwargs['hosts'])
            data,mime=media_files[url.rsplit('/',1)[-1]]
            return data,{'content-type':mime}
        rows=[]
        for i in range(123):
            created=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=i)).isoformat()
            row={'id':f'LOCAL_TEST_MESSAGE_{i:03d}','conversation_id':'LOCAL_TEST_CONVERSATION','user_id':'LOCAL_TEST_CUSTOMER',
                 'actor_type':'user' if i%3 else 'agent','actor_id':'LOCAL_TEST_CUSTOMER' if i%3 else 'LOCAL_TEST_HUMAN',
                 'message_type':'normal','message_source':'web','created_time':created,
                 'message_parts':[{'text':{'content':f'本地 UI 合成测试消息 {i+1}。这不是客户真实会话。'}}]}
            if i==122:row['message_parts']=[{'text':{'content':'<img src=x onerror="window.__xss=true"> 请说明测试停车方案，再附 PDF。'}}]
            if i==120:row['message_parts']=[{'image':{'url':f'https://{media_host}/map.png'}}]
            if i==121:row['message_parts']=[{'file':{'url':f'https://{media_host}/guide.mp4','name':'本地指引.mp4','file_size':len(mp4),'content_type':'video/mp4'}}]
            if i==122:row['message_parts'] += [{'file':{'url':f'https://{media_host}/{name}','name':name}} for name in ('guide.pdf','faq.md')]
            if i in (116,117,118,119):
                row.update(actor_type='system',actor_id='LOCAL_TEST_SYSTEM')
            if i==116:row['message_parts']=[{'text':{'content':'Conversation was assigned to group LOCAL_TEST by Topic Group mapping'}}]
            if i==117:row['message_parts']=[{'text':{'content':'text: 您有張測試充電券未使用！\n您好！\n\n請在到期前使用測試優惠券。\n離場前在 App「優惠券」按「立即使用」即可。\n\n此為合成資料，不會領取或使用真實優惠券。\n\ntype: button\naction.type: link\naction.text: 查看測試優惠券\naction.url: https://example.com/coupon'}}]
            if i==118:
                row['message_parts']=[{'text':{'content':'選擇停車方案（本地合成卡片）'}}]
                row['reply_parts']=[{'template_content':{'type':'carousel','sections':[{'name':'cards','parts':[
                    {'template_content':{'type':'carousel_card_default','sections':[
                        {'name':'hero_image','parts':[{'image':{'url':f'https://{media_host}/map.png'}}]},
                        {'name':'title','parts':[{'text':{'content':name}}]},
                        {'name':'description','parts':[{'text':{'content':'請按需要選擇；管理頁僅展示客戶側選項。'}}]},
                        {'name':'view','parts':[{'url_button':{'label':'查看詳情','url':'https://example.com/parking'}}]},
                        {'name':'callback','parts':[{'callback':{'label':'選擇方案','payload':'LOCAL_TEST_ONLY'}}]}
                    ]}} for name in ('日租方案','月租方案')]}]}}]
            if i==119:
                row['message_parts']=[{'help_text':{'content':'<img src=x onerror="window.__xss=true"> 以下按钮仅展示。'}}]
                row['reply_parts']=[{'collection':{'sub_parts':[{'quick_reply_button':{'label':'需要協助'}},{'url_button':{'label':'危险地址','url':'javascript:window.__xss=true'}}]}}]
            rows.append(row);svc.insert_message(c,normalize_message(row,c['platform_id']))
            if i==117:
                # Reproduce a legacy cache written before rich-part support.
                svc.db.run('UPDATE messages SET parts=? WHERE conversation=? AND platform_id=?',(svc.db.seal([{'type':'text','text':row['message_parts'][0]['text']['content']}]),c['id'],row['id']))
        svc.db.run('UPDATE conversations SET sync_complete=1 WHERE id=?',(c['id'],))
        histories={c['platform_id']:rows}
        for suffix,name,mode in [('SECOND','林小姐的停车咨询与长期车位方案测试','auto'),('THIRD','Alex Chen','manual')]:
            extra=svc.add_conversation('LOCAL_CONV_'+suffix,'LOCAL_USER_'+suffix)
            row={'id':'LOCAL_MSG_'+suffix,'conversation_id':extra['platform_id'],'user_id':extra['user_id'],
                 'actor_type':'user','actor_id':extra['user_id'],'message_type':'normal','created_time':datetime.now(timezone.utc).isoformat(),
                 'message_parts':[{'text':{'content':'想了解月租方案，周末能否进出停车场？这是一条用于检查省略号的长消息。'}}]}
            svc.insert_message(extra,normalize_message(row,extra['platform_id']))
            svc.db.run('UPDATE conversations SET sync_complete=1,mode=? WHERE id=?',(mode,extra['id']))
            svc.db.run('INSERT INTO customer_profiles VALUES(?,?,?,?)',(extra['tenant'],extra['user_id'],svc.db.seal(name),time.time()))
            histories[extra['platform_id']]=[row]
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
        with patch('lsp.providers.agents',return_value=[{'id':'LOCAL_TEST_AGENT','name':'本地测试坐席'}]),patch('lsp.providers.conversation',side_effect=lambda s,cid:{'conversation_id':cid}),patch('lsp.providers.history_pages',side_effect=lambda s,cid,*args:iter([histories[cid].copy()])),patch('lsp.providers.upload',side_effect=lambda s,a,data: ({'url':'https://local-test.example/image.png'} if a['kind']=='image' else {'file_hash':'LOCAL_TEST_HASH','file_security_status':'SAFE_FILE'},'sendable')),patch('lsp.providers.send_message',side_effect=send),patch('lsp.providers.openai',side_effect=model),patch('lsp.providers.freshchat',return_value={'id':'LOCAL_TEST_CUSTOMER','first_name':'本地测试客户'}),patch('lsp.providers.freshdesk',return_value={'id':999,'status':2,'requester_id':42}),patch('lsp.app.security.request',side_effect=media_request):
            svc.start()
            print('LOCAL SYNTHETIC UI TEST ONLY http://127.0.0.1:8128 — password: LOCAL-BROWSER-TEST-ONLY',flush=True)
            serve(app,host='127.0.0.1',port=8128,threads=4)
