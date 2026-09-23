"""Local synthetic tests only. No test proves tenant, connector, or customer delivery."""
import base64
import copy
import io
import json
import os
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from lsp.app import create_app
from lsp import providers, security
from lsp.service import Service, normalize_message
from lsp.settings import DEFAULTS, Settings, tenant_id
from lsp.store import dump


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.rsa.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'TESTING':True,'DATABASE_PATH':self.temp.name+'/db.sqlite3','FILE_DIRECTORY':self.temp.name+'/files',
                       'CONFIG_MASTER_KEY':Fernet.generate_key().decode(),'ADMIN_INITIAL_PASSWORD':'local-test-password-123'}
        self.app=create_app(self.config,start_worker=False)
        self.client=self.app.test_client()
        self.svc=self.app.extensions['service'];self.db=self.svc.db
        self.svc.settings.save({'platform_api_base_url':'https://demo.freshchat.com','freshchat_token':'synthetic-platform-token',
          'reply_actor_id':'agent-self','source_mapping':{'web':'Webchat','whatsapp':'WhatsApp'},'allowed_channels':['Webchat'],
          'test_identity_allowlist':['user:user-1'],'freshchat_public_key':self.pem,'public_base_url':'https://demo.example.com',
          'openai_api_key':'synthetic-openai-key','openai_model':'account-model','freshdesk_domain':'https://demo.freshdesk.com',
          'freshdesk_api_key':'synthetic-desk-key','requester_mapping':{'user-1':42},'scheduler_token':'s'*40})
        self.c=self.svc.add_conversation('conv-1','user-1','web','topic-only')
        self.auth=self.client.get('/api/auth').json['csrf']
        login=self.client.post('/api/login',json={'password':self.config['ADMIN_INITIAL_PASSWORD']},headers={'X-CSRF-Token':self.auth})
        self.assertEqual(login.status_code,200,login.json)
        self.csrf=login.json['csrf']
        self.netpatch=patch('socket.create_connection',side_effect=AssertionError('Unexpected network in local tests'))
        self.netpatch.start();self.addCleanup(self.netpatch.stop)

    def call(self,path,method='POST',data=None):
        return self.client.open(path,method=method,json={} if data is None else data,headers={'X-CSRF-Token':self.csrf})

    def message(self,mid='m1',actor='user',created='2030-01-01T00:00:01Z',private=False,**extra):
        return {'id':mid,'conversation_id':'conv-1','user_id':'user-1','message_source':'web','channel_id':'topic-only',
                'actor_type':actor,'actor_id':'user-1' if actor=='user' else 'agent-other','message_type':'private' if private else 'normal',
                'created_time':created,'message_parts':[{'text':{'content':'停车位置在哪里'}}],**extra}

    def event(self,message=None):
        return {'action':'message_create','data':{'message':message or self.message()}}

    def webhook(self,payload=None,signature=True,mutate=False):
        raw=json.dumps(payload or self.event(),ensure_ascii=False,indent=2).encode()
        sig=base64.b64encode(self.rsa.sign(raw,padding.PKCS1v15(),hashes.SHA256())).decode()
        return self.client.post('/api/webhooks/freshchat',data=raw+b' ' if mutate else raw,headers={'Content-Type':'application/json','X-Freshchat-Signature':sig if signature else '', 'X-Freshchat-Payload-Version':'test-1','X-Retry-Count':'0'})

    def enable(self):
        for channel,cap in [('*','openai'),('Webchat','inbound'),('Webchat','history'),('Webchat','manual_text')]:
            self.svc.settings.record(channel,cap,'passed','synthetic',{'local_only':True})
        self.svc.settings.save({'auto_reply_enabled':True})
        self.c=self.svc.conv(self.c['id'])

    def add_history(self,message=None):
        self.svc.insert_message(self.svc.conv(self.c['id']),normalize_message(message or self.message(),'conv-1'))

    def text(self,text='测试回复'):
        return {'type':'text','text':text,'asset_id':None}

    def model_response(self,plan=None,status='completed'):
        return {'status':status,'id':'resp_test','output':[{'type':'reasoning'},{'type':'message','content':[{'type':'output_text','text':json.dumps(plan or {'messages':[self.text()],'needs_human':False,'ticket_reason':None})}]}],
                'usage':{'input_tokens':100,'output_tokens':50,'total_tokens':150}}

    def pair_test_account(self, channel='WhatsApp', suffix='wa'):
        started=self.call('/api/test-discovery',data={'channel':channel,'confirm_auto_reply':True})
        self.assertEqual(started.status_code,200,started.json)
        code=started.json['enrollment']['code']
        msg=self.message('pair-'+suffix,conversation_id='test-conv-'+suffix,user_id='test-user-'+suffix,
                         actor_id='test-user-'+suffix,message_source=channel.lower(),message_parts=[{'text':{'content':code}}])
        self.assertEqual(self.webhook(self.event(msg)).json['status'],'discovered')
        return msg

    def run_pairing_jobs(self, rows, before_model=None):
        def model(*args):
            if before_model:before_model()
            return self.model_response(),'local-pairing-request'
        with patch('lsp.providers.conversation',side_effect=lambda s,cid:{'conversation_id':cid}), \
             patch('lsp.providers.history_pages',side_effect=lambda s,cid,*args:iter([rows[cid]])), \
             patch('lsp.providers.openai',side_effect=model), \
             patch('lsp.providers.send_message',return_value={'id':'paired-reply'}) as send:
            counts=self.svc.drain(20)
        self.assertEqual(counts['failed'],0,[(j['kind'],j['error']) for j in self.db.all("SELECT kind,error FROM jobs WHERE state='failed'")])
        return send.call_count

    def test_pairing_requires_admin_csrf_opt_in_and_configuration(self):
        self.assertEqual(self.app.test_client().get('/api/test-discovery').status_code,401)
        self.assertEqual(self.client.post('/api/test-discovery',json={}).status_code,403)
        for body in ({},{'channel':'WhatsApp'},{'channel':'WhatsApp','confirm_auto_reply':False},
                     {'channel':'unknown','confirm_auto_reply':True},{'channel':[],'confirm_auto_reply':True}):
            result=self.call('/api/test-discovery',data=body)
            self.assertEqual(result.status_code,400,result.json)
        self.svc.settings.save({'clear_secrets':['openai_api_key']})
        self.assertEqual(self.call('/api/test-discovery',data={'channel':'WhatsApp','confirm_auto_reply':True}).status_code,409)
        self.assertEqual(self.db.one('SELECT count(*) n FROM jobs')['n'],0)

    def test_short_binding_listens_for_redacted_candidates_then_selects_one(self):
        started = self.call('/api/test-discovery/bind-next', data={'channel': 'WhatsApp'})
        self.assertEqual(started.status_code, 200, started.json)
        msg = self.message('candidate-1', conversation_id='candidate-conv', user_id='candidate-user',
                           message_source='whatsapp', message_parts=[{'text': {'content': '真实客户问题'}}])
        result = self.webhook(self.event(msg))
        self.assertEqual(result.json, {'status': 'candidate'})
        self.assertEqual(self.db.one('SELECT count(*) n FROM messages')['n'], 0)
        self.assertEqual(self.db.one('SELECT count(*) n FROM events')['n'], 0)
        self.assertEqual(self.db.one('SELECT count(*) n FROM jobs')['n'], 0)
        public = self.call('/api/test-discovery', 'GET').json
        self.assertEqual(len(public['candidates']), 1)
        self.assertNotIn('真实客户问题', json.dumps(public, ensure_ascii=False))
        self.assertEqual(self.webhook(self.event(msg)).json, {'status': 'candidate'})
        self.assertEqual(len(self.call('/api/test-discovery', 'GET').json['candidates']), 1)

        selected = self.call('/api/test-discovery/select', data={'conversation_id': 'candidate-conv'})
        self.assertEqual(selected.status_code, 200, selected.json)
        local = self.svc.conv(selected.json['conversation_id'])
        self.assertEqual(local['user_id'], 'candidate-user')
        self.assertEqual(local['mode'], 'off')
        self.assertIn('user:candidate-user', self.svc.settings.get()[0]['test_identity_allowlist'])
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='activate_test'")['n'], 1)
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='sync'")['n'], 0)
        with patch('lsp.providers.conversation', return_value={'conversation_id': 'candidate-conv'}), \
             patch('lsp.providers.history_pages', return_value=iter([[msg]])), \
             patch('lsp.providers.openai', return_value=(self.model_response(), 'candidate-openai')):
            self.assertEqual(self.svc.drain(20)['failed'], 0)
        self.assertFalse(self.svc.settings.get()[0]['auto_reply_enabled'])
        switched = self.call('/api/conversations/%s/mode' % local['id'], method='PUT', data={'mode': 'auto'})
        self.assertEqual(switched.status_code, 200, switched.json)
        self.assertEqual(self.svc.conv(local['id'])['mode'], 'auto')
        self.assertEqual(self.call('/api/test-discovery/select', data={'conversation_id': 'candidate-conv'}).status_code, 404)

    def test_selected_agent_auto_collects_new_conversation_and_exposes_event_payload(self):
        self.svc.settings.save({'test_identity_allowlist': []})
        resumed = self.call('/api/settings', method='PUT', data={'reply_actor_id': 'agent-self'})
        self.assertEqual(resumed.status_code, 200, resumed.json)
        msg = self.message('inbox-1', conversation_id='inbox-conv', user_id='inbox-user',
                           message_source='web', message_parts=[{'text': {'content': '新的客户问题'}}])
        result = self.webhook(self.event(msg))
        self.assertEqual(result.json['status'], 'accepted')
        conversation = self.db.one("SELECT * FROM conversations WHERE platform_id='inbox-conv'")
        self.assertEqual(conversation['mode'], 'off')
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='sync' AND conversation=?", (conversation['id'],))['n'], 1)
        events = self.call('/api/events', method='GET').json['events']
        self.assertEqual(events[0]['platform_id'], 'inbox-1')
        self.assertEqual(events[0]['payload']['data']['message']['conversation_id'], 'inbox-conv')
        self.assertIn('新的客户问题', json.dumps(events[0]['payload'], ensure_ascii=False))
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n'], 0)
        with patch('lsp.providers.conversation', return_value={'conversation_id': 'inbox-conv'}), \
             patch('lsp.providers.history_pages', return_value=iter([[msg]])):
            self.assertEqual(self.svc.drain(20)['failed'], 0)
        self.svc.settings.record('*', 'openai', 'passed', 'synthetic', {'local_only': True})
        switched = self.call('/api/conversations/%s/mode' % conversation['id'], method='PUT', data={'mode': 'auto'})
        self.assertEqual(switched.status_code, 200, switched.json)
        self.assertEqual(self.svc.conv(conversation['id'])['mode'], 'auto')
        self.assertFalse(self.svc.settings.get()[0]['auto_reply_enabled'])

        # Known conversation agent events must still cancel AI without a customer allowlist.
        self.webhook(self.event({**msg, 'id': 'inbox-followup'}))
        self.assertEqual(self.webhook(self.event({**msg, 'id': 'inbox-followup'})).json['status'], 'duplicate')
        agent = {**msg, 'id': 'human-reply', 'actor_type': 'agent', 'actor_id': 'agent-other'}
        agent.pop('user_id')
        self.assertEqual(self.webhook(self.event(agent)).json['status'], 'accepted')
        self.assertEqual(self.svc.conv(conversation['id'])['mode'], 'manual')
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE origin='ai' AND state IN ('queued','pending')")['n'], 0)

    def test_auto_discovery_second_channel_does_not_reset_first_conversation(self):
        self.svc.settings.save({'test_identity_allowlist': [], 'allowed_channels': [], 'source_mapping': {'whatsapp': 'WhatsApp'}})
        self.call('/api/settings', method='PUT', data={'reply_actor_id': 'agent-self'})
        self.svc.settings.record('*', 'openai', 'passed', 'synthetic', {'local_only': True})
        revision = self.svc.settings.get()[1]
        for channel in ('whatsapp', 'wechat'):
            msg = self.message(channel, conversation_id=channel, user_id='user-'+channel, message_source=channel)
            self.assertEqual(self.webhook(self.event(msg)).json['status'], 'accepted')
            c = self.db.one('SELECT * FROM conversations WHERE platform_id=?', (channel,))
            self.assertEqual(c['mode'], 'off')
            with patch('lsp.providers.conversation', return_value={'conversation_id': channel}), \
                 patch('lsp.providers.history_pages', return_value=iter([[msg]])):
                self.svc.drain()
            switched = self.call('/api/conversations/%s/mode' % c['id'], 'PUT', {'mode': 'auto'})
            self.assertEqual(switched.status_code, 200, switched.json)
            self.assertEqual(self.svc.settings.get()[1], revision)
        self.assertEqual(self.db.one("SELECT count(*) n FROM conversations WHERE mode='auto'")['n'], 2)
        first = self.db.one("SELECT id FROM conversations WHERE platform_id='whatsapp'")['id']
        self.call('/api/conversations/%s/mode' % first, 'PUT', {'mode': 'manual'})
        self.assertEqual(self.db.one("SELECT mode FROM conversations WHERE platform_id='wechat'")['mode'], 'auto')
        self.call('/api/test-discovery', 'DELETE')
        self.assertEqual(self.db.one("SELECT count(*) n FROM conversations WHERE mode='auto'")['n'], 0)
        self.assertEqual(self.webhook().json['status'], 'ignored')

    def test_auto_discovery_assignment_roles_and_pause(self):
        self.svc.settings.save({'test_identity_allowlist': []})
        self.call('/api/settings', method='PUT', data={'reply_actor_id': 'agent-self'})
        msg = self.message('new-customer', conversation_id='new-customer', user_id='new-customer')
        with patch('lsp.providers.conversation') as read:
            for extra in ({'assigned_agent_id': 'agent-other'}, {'actor_type': 'agent'}, {'private': True}):
                self.assertEqual(self.webhook(self.event({**msg, **extra})).json['status'], 'ignored')
            read.assert_not_called()
        self.assertIsNone(self.db.one("SELECT id FROM conversations WHERE platform_id='new-customer'"))
        self.assertEqual(self.webhook(self.event({**msg, 'assigned_agent_id': 'agent-self'})).json['status'], 'accepted')
        self.assertNotIn('new-customer', self.db.one('SELECT payload FROM events')['payload'])
        self.assertEqual(self.app.test_client().get('/api/events').status_code, 401)
        self.call('/api/test-discovery', 'DELETE')
        self.assertFalse(self.call('/api/test-discovery', 'GET').json['auto_discovery'])
        self.assertEqual(self.webhook(self.event({**msg, 'id': 'paused'})).json['status'], 'ignored')
        self.call('/api/settings', method='PUT', data={'reply_actor_id': 'agent-self'})
        self.assertTrue(self.call('/api/test-discovery', 'GET').json['auto_discovery'])

    def test_auto_discovery_resolves_assignment_before_history_and_customer_from_actor(self):
        self.call('/api/settings', 'PUT', {'reply_actor_id': 'agent-self'})
        msg = self.message('discovered', conversation_id='assigned-elsewhere', actor_id='actor-customer')
        msg.pop('user_id')
        result = self.webhook(self.event(msg))
        c = self.svc.conv(result.json['conversation_id'])
        self.assertEqual(c['user_id'], 'actor-customer')
        with patch('lsp.providers.conversation', return_value={'assigned_agent_id': 'agent-other'}), \
             patch('lsp.providers.history_pages') as history:
            self.assertEqual(self.svc.drain()['failed'], 1)
            history.assert_not_called()
        self.assertFalse(self.svc.conv(c['id'])['sync_complete'])
        self.assertNotIn(c['id'], [row['id'] for row in self.call('/api/conversations', 'GET').json['conversations']])
        self.assertEqual(self.call('/api/conversations/%s/mode' % c['id'], 'PUT', {'mode': 'auto'}).status_code, 409)
        event = {**msg, 'id': 'reassigned', 'assigned_agent_id': 'agent-self'}
        self.assertEqual(self.webhook(self.event(event)).json['status'], 'accepted')
        self.assertIn(c['id'], [row['id'] for row in self.call('/api/conversations', 'GET').json['conversations']])

    def test_pairing_filters_signature_roles_other_customers_and_consumed_code(self):
        start=self.call('/api/test-discovery',data={'channel':'WhatsApp','confirm_auto_reply':True}).json
        code=start['enrollment']['code']
        raw=self.message('pairing',conversation_id='pairing-c',user_id='pairing-u',actor_id='pairing-u',message_source='whatsapp',message_parts=[{'text':{'content':code}}])
        self.assertEqual(self.webhook(self.event(raw),signature=False).status_code,401)
        self.assertEqual(self.webhook(self.event(raw),mutate=True).status_code,401)
        before={t:self.db.one(f'SELECT count(*) n FROM {t}')['n'] for t in ('messages','jobs','conversations','events')}
        for changes in ({'actor_type':'agent'},{'message_type':'private'},{'private':True},{'user_id':''},
                        {'message_parts':[{'text':{'content':'LSP-DEMO-TEST-001'}}]},
                        {'message_parts':[{'text':{'content':code+' extra'}}]}):
            self.assertEqual(self.webhook(self.event({**raw,**changes})).json['status'],'ignored')
        self.assertEqual(before,{t:self.db.one(f'SELECT count(*) n FROM {t}')['n'] for t in before})
        self.assertEqual(self.webhook(self.event(raw)).json,{'status':'discovered'})
        self.assertEqual(self.webhook(self.event(raw)).json,{'status':'discovered'})
        copied={**raw,'id':'copied','conversation_id':'other-c','user_id':'other-u'}
        self.assertEqual(self.webhook(self.event(copied)).json,{'status':'discovered'})
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='activate_test'")['n'],1)
        self.assertEqual(self.svc.settings.get()[0]['test_identity_allowlist'],['user:pairing-u'])
        public=self.call('/api/test-discovery','GET').json
        self.assertNotIn(code,json.dumps(public));self.assertNotIn('synthetic',json.dumps(public))
        self.assertIsNone(public['enrollment'])
        self.assertFalse(public['bindings'][0]['enabled'])

    def test_pairing_two_channels_activate_without_faking_acceptance_and_takeover(self):
        wa=self.pair_test_account();rows={wa['conversation_id']:[wa]}
        self.assertEqual(self.run_pairing_jobs(rows),1)
        self.assertFalse(self.svc.settings.passed('WhatsApp','manual_text'))
        self.assertFalse(self.svc.settings.passed('WhatsApp','ai_text'))
        self.assertEqual(self.svc.discovery()['bindings']['WhatsApp']['status'],'active')
        wc=self.pair_test_account('WeChat','wc');rows[wc['conversation_id']]=[wc]
        self.assertEqual(self.run_pairing_jobs(rows),1)
        state=self.svc.discovery();wa_id=state['bindings']['WhatsApp']['local_id'];wc_id=state['bindings']['WeChat']['local_id']
        self.assertEqual(set(self.svc.settings.get()[0]['test_identity_allowlist']),{'user:test-user-wa','user:test-user-wc'})
        self.assertTrue(all(b['enabled'] for b in self.call('/api/test-discovery','GET').json['bindings']))
        takeover=self.call('/api/test-discovery/mode','PUT',{'channel':'WhatsApp','enabled':False})
        self.assertEqual(takeover.status_code,200,takeover.json)
        self.assertEqual(self.svc.conv(wa_id)['mode'],'manual');self.assertEqual(self.svc.conv(wc_id)['mode'],'auto')
        qwa={**wa,'id':'wa-question','message_parts':[{'text':{'content':'入口在哪？'}}]}
        qwc={**wc,'id':'wc-question','message_parts':[{'text':{'content':'價格呢？'}}]}
        self.webhook(self.event(qwa));self.webhook(self.event(qwc))
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate' AND state='queued' AND conversation=?",(wa_id,))['n'],0)
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate' AND state='queued' AND conversation=?",(wc_id,))['n'],1)
        cross={**qwa,'id':'cross','conversation_id':'wrong-channel','message_source':'wechat'}
        self.assertEqual(self.webhook(self.event(cross)).json['status'],'ignored')
        reopened={**qwa,'id':'reopened','conversation_id':'wa-reopened'}
        self.assertEqual(self.webhook(self.event(reopened)).json['status'],'accepted')
        reopened_c=self.db.one("SELECT * FROM conversations WHERE platform_id='wa-reopened'")
        self.assertNotEqual(reopened_c['mode'],'auto')
        queued_before=self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n']
        self.assertEqual(self.call('/api/test-discovery/mode','PUT',{'channel':'WhatsApp','enabled':True}).status_code,200)
        self.assertEqual(self.svc.conv(reopened_c['id'])['mode'],'auto')
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n'],queued_before)
        self.svc.manual_send(wc_id,[self.text('人工回答')])
        self.assertTrue(self.svc.discovery()['bindings']['WeChat']['paused'])
        self.assertEqual(self.svc.conv(wa_id)['mode'],'auto')

    def test_pairing_expiry_rotation_configuration_and_stop(self):
        start=self.call('/api/test-discovery',data={'channel':'WhatsApp','confirm_auto_reply':True}).json['enrollment']
        with patch('lsp.service.time.time',return_value=start['expires']+1):
            self.assertIsNone(self.call('/api/test-discovery','GET').json['enrollment'])
        first=self.call('/api/test-discovery',data={'channel':'WhatsApp','confirm_auto_reply':True}).json['enrollment']['code']
        second=self.call('/api/test-discovery',data={'channel':'WeChat','confirm_auto_reply':True}).json['enrollment']['code']
        self.assertNotEqual(first,second)
        self.svc.settings.save({'freshchat_token':'new-local-test-token'})
        self.assertIsNone(self.call('/api/test-discovery','GET').json['enrollment'])
        self.assertEqual(self.call('/api/test-discovery','GET').json['bindings'],[])
        wa=self.pair_test_account()
        self.assertEqual(self.call('/api/test-discovery','DELETE').status_code,200)
        self.assertEqual(self.svc.settings.get()[0]['test_identity_allowlist'],[])
        self.assertFalse(self.svc.settings.get()[0]['auto_reply_enabled'])
        self.assertEqual(self.webhook(self.event(wa)).json['status'],'ignored')
        self.assertEqual(self.svc.drain()['processed'],0)

    def test_pairing_manual_during_startup_stays_manual_and_restart_does_not_replay(self):
        wa=self.pair_test_account();rows={wa['conversation_id']:[wa]}
        def takeover():
            result=self.call('/api/test-discovery/mode','PUT',{'channel':'WhatsApp','enabled':False})
            self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(self.run_pairing_jobs(rows,takeover),0)
        binding=self.svc.discovery()['bindings']['WhatsApp']
        self.assertTrue(binding['paused']);self.assertEqual(self.svc.conv(binding['local_id'])['mode'],'manual')
        other=create_app(self.config,start_worker=False).extensions['service']
        self.assertTrue(other.discovery()['bindings']['WhatsApp']['paused'])
        self.assertEqual(other.drain()['selected'],0)
        self.assertEqual(self.call('/api/test-discovery/mode','PUT',{'channel':'WhatsApp','enabled':True}).status_code,200)
        self.assertEqual(self.svc.drain()['selected'],0)

    def test_pairing_replacing_one_channel_preserves_other_and_revokes_old_account(self):
        wa=self.pair_test_account();rows={wa['conversation_id']:[wa]};self.run_pairing_jobs(rows)
        wc=self.pair_test_account('WeChat','wc');rows[wc['conversation_id']]=[wc];self.run_pairing_jobs(rows)
        self.call('/api/test-discovery/mode','PUT',{'channel':'WeChat','enabled':False})
        replacement=self.pair_test_account('WhatsApp','wa-new');rows[replacement['conversation_id']]=[replacement]
        self.run_pairing_jobs(rows)
        state=self.svc.discovery()['bindings']
        self.assertTrue(state['WeChat']['paused']);self.assertEqual(state['WhatsApp']['user_id'],'test-user-wa-new')
        self.assertNotIn('user:test-user-wa',self.svc.settings.get()[0]['test_identity_allowlist'])
        self.assertEqual(self.webhook(self.event({**wa,'id':'old-client','message_parts':[{'text':{'content':'old account question'}}]})).json['status'],'ignored')
        self.assertEqual(self.svc.conv(state['WeChat']['local_id'])['mode'],'manual')

    def test_pairing_stop_during_model_check_cannot_reenable_or_send(self):
        wa=self.pair_test_account()
        def stop(*args):
            self.assertEqual(self.call('/api/test-discovery','DELETE').status_code,200)
            return self.model_response(),'stopped-check'
        with patch('lsp.providers.conversation',return_value={'conversation_id':wa['conversation_id']}), \
             patch('lsp.providers.history_pages',return_value=iter([[wa]])), \
             patch('lsp.providers.openai',side_effect=stop),patch('lsp.providers.send_message') as send:
            self.svc.drain();send.assert_not_called()
        self.assertFalse(self.svc.settings.get()[0]['auto_reply_enabled'])
        self.assertEqual(self.svc.discovery()['bindings'],{})
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='send'")['n'],0)

    def test_pairing_history_human_reply_during_start_keeps_manual(self):
        wa=self.pair_test_account()
        human={**wa,'id':'human-after-code','actor_type':'agent','actor_id':'real-test-agent',
               'created_time':'2030-01-01T00:00:02Z','message_parts':[{'text':{'content':'人工处理'}}]}
        self.assertEqual(self.run_pairing_jobs({wa['conversation_id']:[wa,human]}),0)
        self.assertTrue(self.svc.discovery()['bindings']['WhatsApp']['paused'])

    def test_pairing_source_conflict_and_model_failure_do_not_send(self):
        start=self.call('/api/test-discovery',data={'channel':'WeChat','confirm_auto_reply':True}).json['enrollment']
        bad=self.message(conversation_id='bad-source',user_id='bad-user',message_source='whatsapp',message_parts=[{'text':{'content':start['code']}}])
        self.assertEqual(self.webhook(self.event(bad)).json['status'],'discovered')
        self.assertEqual(self.call('/api/test-discovery','GET').json['enrollment']['status'],'failed')
        self.assertEqual(self.db.one('SELECT count(*) n FROM jobs')['n'],0)
        wa=self.pair_test_account()
        with patch('lsp.providers.conversation',return_value={'conversation_id':wa['conversation_id']}), \
             patch('lsp.providers.history_pages',return_value=iter([[wa]])), \
             patch('lsp.providers.openai',side_effect=security.Problem('model_incomplete','模型输出未完成',409)), \
             patch('lsp.providers.send_message') as send:
            self.assertEqual(self.svc.drain()['failed'],1);send.assert_not_called()
        result=self.call('/api/test-discovery','GET').json['bindings'][0]
        self.assertEqual(result['status'],'failed');self.assertFalse(result['enabled'])
        self.assertFalse(self.svc.settings.get()[0]['auto_reply_enabled'])

    def test_settings_encryption_masking_clear_restart(self):
        public=self.call('/api/settings','GET').json
        self.assertEqual(public['values']['openai_api_key'],'');self.assertTrue(public['secrets']['openai_api_key'])
        self.assertNotIn('synthetic-openai-key',Path(self.config['DATABASE_PATH']).read_bytes().decode(errors='ignore'))
        self.svc.settings.save({'openai_api_key':''})
        self.assertEqual(self.svc.settings.get()[0]['openai_api_key'],'synthetic-openai-key')
        other=create_app(self.config,start_worker=False).extensions['service']
        self.assertEqual(other.settings.get()[0]['openai_api_key'],'synthetic-openai-key')
        other.settings.save({'clear_secrets':['openai_api_key']})
        self.assertFalse(other.settings.public()['secrets']['openai_api_key'])

    def test_quick_start_model_defaults_and_existing_config_migration(self):
        self.assertEqual(DEFAULTS['openai_model'], 'gpt-6-astra')
        self.enable()
        s, revision = self.svc.settings.get()
        s['openai_model'] = ''  # Existing persisted configuration before this change.
        self.db.run("UPDATE meta SET value=? WHERE key='settings'", (self.db.seal({'revision':revision,'values':s}),))
        settings = Settings(self.db)
        current, new_revision = settings.get()
        self.assertEqual(current['openai_model'], DEFAULTS['openai_model'])
        self.assertGreater(new_revision, revision)
        self.assertFalse(current['auto_reply_enabled'])
        self.assertEqual(current['openai_api_key'], 'synthetic-openai-key')
        self.assertEqual(Settings(self.db).get(), settings.get())
        settings.save({'openai_model':'account-specific-model'})
        self.assertEqual(Settings(self.db).get()[0]['openai_model'], 'account-specific-model')
        result = self.call('/api/settings','PUT',{'openai_model':'  '})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['values']['openai_model'],DEFAULTS['openai_model'])
        self.assertEqual(result.json['values']['openai_api_key'],'')

    def test_quick_start_agents_pagination_filtering_and_errors(self):
        responses=[{'agents':[{'id':'a1','first_name':'Test','last_name':'Agent','email':'private@example.test'},
                             {'id':'a2','is_deleted':True}], 'pagination':{'total_pages':2}},
                   {'agents':[{'id':'a3','first_name':'Second'}, {'id':'a4','is_deactivated':True}], 'pagination':{'total_pages':2}}]
        with patch('lsp.providers.freshchat',side_effect=responses) as provider:
            result=self.call('/api/agents','GET')
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['agents'],[{'id':'a1','name':'Test Agent'},{'id':'a3','name':'Second'}])
        self.assertIn('page=2&',provider.call_args_list[1].args[1])
        self.assertNotIn('private@example.test',json.dumps(result.json))
        self.assertEqual(self.app.test_client().get('/api/agents').status_code,401)
        for response in ({'agents':[{'id':[]}]},{'agents':[{'id':'a1'}]}, {'wrong':[]}):
            with patch('lsp.providers.freshchat',return_value=response):
                error=self.call('/api/agents','GET')
                self.assertEqual(error.status_code,502,error.json)
        with patch('lsp.providers.freshchat',side_effect=security.Problem('provider_forbidden','平台权限不足',403)):
            self.assertEqual(self.call('/api/agents','GET').status_code,403)

    def test_quick_start_test_scope_is_explicit_idempotent_and_tenant_bound(self):
        self.enable()
        original=self.svc.settings.get()[0]['test_identity_allowlist'][:]
        c=self.svc.add_conversation('wa-conversation','wa-user','observed-wa')
        other=self.svc.add_conversation('other-conversation','other-user','observed-wa')
        url=f"/api/conversations/{c['id']}/test-access"
        self.assertEqual(self.client.post(url,json={'channel':'WhatsApp'}).status_code,403)
        result=self.call(url,data={'channel':'WhatsApp'})
        self.assertEqual(result.status_code,200,result.json)
        s,revision=self.svc.settings.get()
        self.assertEqual(s['source_mapping']['observed-wa'],'WhatsApp')
        self.assertEqual(s['test_identity_allowlist'],original+['conversation:wa-conversation'])
        self.assertFalse(s['auto_reply_enabled'])
        self.assertEqual(self.db.one('SELECT count(*) n FROM jobs')['n'],0)
        self.svc.send_guard(self.svc.conv(c['id']),s)
        with self.assertRaises(security.Problem):self.svc.send_guard(self.svc.conv(other['id']),s)
        self.assertEqual(self.call(url,data={'channel':'WhatsApp'}).status_code,200)
        self.assertEqual(self.svc.settings.get()[1],revision)
        self.assertEqual(self.call(url,data={'channel':'WeChat'}).status_code,409)
        for invalid in ({'channel':''},{'channel':'unknown'},{'channel':42},{'channel':'Webchat','source':'invented'}):
            self.assertEqual(self.call(url,data=invalid).status_code,400)
        unknown=self.svc.add_conversation('no-source','user-unknown')
        self.assertEqual(self.call(f"/api/conversations/{unknown['id']}/test-access",data={'channel':'Webchat'}).status_code,409)
        self.svc.settings.save({'freshchat_token':'other-synthetic-tenant'})
        self.assertEqual(self.call(url,data={'channel':'WhatsApp'}).status_code,404)

    def test_auth_csrf_origin_and_no_public_file_access(self):
        self.assertEqual(self.app.test_client().get('/api/settings').status_code,401)
        self.assertEqual(self.client.put('/api/settings',json={}).status_code,403)
        self.assertEqual(self.client.put('/api/settings',json={},headers={'X-CSRF-Token':self.csrf,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.app.test_client().get('/api/assets/x/content').status_code,401)
        self.assertEqual(self.call('/api/settings','PUT',{'platform_api_base_url':'http://127.0.0.1'}).status_code,400)
        self.assertEqual(self.call('/api/settings','PUT',{'max_safe_retries':True}).status_code,400)

    def test_settings_origin_through_tunnel_and_local_entry(self):
        # A second tunnel may serve the UI while public_base_url points at the webhook tunnel.
        for base,origin in [
            ('http://previous-tunnel.example','https://previous-tunnel.example'),
            ('http://previous-tunnel.example:443','https://previous-tunnel.example'),
            ('http://127.0.0.1:8127','http://127.0.0.1:8127'),
            ('http://localhost:8127','http://localhost:8127'),
            ('http://[::1]:8127','http://[::1]:8127'),
            ('http://[::1]:443','https://[::1]'),
            ('http://127.0.0.1:8127','https://demo.example.com'),
        ]:
            with self.subTest(base=base,origin=origin):
                client=self.app.test_client()
                nonce=client.get('/api/auth',base_url=base).json['csrf']
                login=client.post('/api/login',base_url=base,json={'password':self.config['ADMIN_INITIAL_PASSWORD']},
                                  headers={'X-CSRF-Token':nonce,'Origin':origin})
                self.assertEqual(login.status_code,200,login.json)
                headers={'X-CSRF-Token':login.json['csrf'],'Origin':origin}
                saved=client.put('/api/settings',base_url=base,json={},headers=headers)
                self.assertEqual(saved.status_code,200,saved.json)
                self.assertEqual(saved.json['values']['public_base_url'],'https://demo.example.com')
                self.assertEqual(saved.json['values']['freshchat_token'],'')
                self.assertEqual(client.put('/api/settings',base_url=base,json={},headers={'Origin':origin}).json['error'],'csrf_invalid')
                for foreign in ('null','https://evil.example','https://previous-tunnel.example.evil.example',
                                'https://previous-tunnel.example:8443'):
                    rejected=client.put('/api/settings',base_url=base,json={},headers={**headers,'Origin':foreign,
                        'X-Forwarded-Host':'evil.example','X-Forwarded-Proto':'https'})
                    self.assertEqual(rejected.status_code,403,rejected.json)
                    self.assertEqual(rejected.json['error'],'origin_invalid')
                self.assertEqual(client.post('/api/logout',base_url=base,json={},headers=headers).status_code,200)

    def test_local_http_cookies_work_with_public_webhook_configured(self):
        for base,secure in [('http://localhost:8127',False),('http://127.0.0.1:8127',False),
                            ('http://[::1]:8127',False),('https://localhost:8127',True),
                            ('http://demo.example.com',True),('https://demo.example.com',True)]:
            with self.subTest(base=base):
                client=self.app.test_client()
                auth=client.get('/api/auth',base_url=base)
                self.assertEqual('; Secure' in auth.headers['Set-Cookie'],secure)
                login=client.post('/api/login',base_url=base,json={'password':self.config['ADMIN_INITIAL_PASSWORD']},
                                  headers={'X-CSRF-Token':auth.json['csrf']})
                self.assertEqual(login.status_code,200,login.json)
                self.assertEqual('; Secure' in login.headers['Set-Cookie'],secure)
                self.assertIn('HttpOnly',login.headers['Set-Cookie'])
                self.assertIn('SameSite=Strict',login.headers['Set-Cookie'])

    def test_unselected_customers_never_persist_or_enqueue_even_with_same_marker(self):
        self.enable()
        tables=('conversations','messages','events','jobs','logs','checks','usage','tickets')
        before={t:self.db.one(f'SELECT count(*) n FROM {t}')['n'] for t in tables}
        with patch('lsp.providers.conversation') as history,patch('lsp.providers.openai') as model,patch('lsp.providers.freshdesk') as desk:
            for i in range(100):
                msg=self.message(f'outside-{i}',conversation_id=f'outside-conv-{i}',user_id=f'outside-user-{i}',
                                 nickname='LSP测试客户',message_parts=[{'text':{'content':'LSP-DEMO-TEST-001'}}])
                result=self.webhook(self.event(msg))
                self.assertEqual(result.status_code,200,result.json)
                self.assertEqual(result.json,{'status':'ignored','reason':'outside_test_scope'})
            self.assertEqual(self.svc.drain()['selected'],0)
            history.assert_not_called();model.assert_not_called();desk.assert_not_called()
        self.assertEqual({t:self.db.one(f'SELECT count(*) n FROM {t}')['n'] for t in tables},before)
        self.assertEqual(self.webhook().json['status'],'accepted')
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n'],1)

    def test_empty_scope_ignores_messages_but_never_bypasses_signature(self):
        self.svc.settings.save({'test_identity_allowlist':[]})
        self.assertEqual(self.webhook(signature=False).status_code,401)
        self.assertEqual(self.webhook(mutate=True).status_code,401)
        self.assertEqual(self.webhook().json,{'status':'ignored','reason':'outside_test_scope'})
        self.assertEqual(self.webhook({'action':'conversation_update','private_data':'DO-NOT-STORE'}).json,
                         {'status':'ignored','reason':'non_message_event'})
        for table in ('messages','events','jobs','logs'):
            self.assertEqual(self.db.one(f'SELECT count(*) n FROM {table}')['n'],0)

    def test_selected_customer_reopen_and_agent_without_user_id(self):
        msg=self.message('reopened-message',conversation_id='reopened-conversation')
        self.assertEqual(self.webhook(self.event(msg)).json['status'],'accepted')
        c=self.db.one("SELECT * FROM conversations WHERE platform_id='reopened-conversation'")
        self.assertEqual(c['user_id'],'user-1')
        self.assertEqual(self.db.one('SELECT origin FROM jobs WHERE conversation=?',(c['id'],))['origin'],'webhook')
        agent=self.message('agent-reply','agent',conversation_id='reopened-conversation')
        del agent['user_id']
        self.assertEqual(self.webhook(self.event(agent)).json['status'],'accepted')
        self.assertEqual(self.svc.conv(c['id'])['mode'],'manual')
        agent['conversation_id']='unknown-agent-conversation'
        self.assertEqual(self.webhook(self.event(agent)).json['status'],'ignored')
        self.assertIsNone(self.db.one("SELECT id FROM conversations WHERE platform_id='unknown-agent-conversation'"))

    def test_single_customer_selection_replaces_scope_and_cancels_pending_work(self):
        self.enable();self.webhook()
        other=self.svc.add_conversation('conv-second','user-2','web')
        url=f"/api/conversations/{other['id']}/test-access"
        result=self.call(url,data={'channel':'Webchat','scope':'customer'})
        self.assertEqual(result.status_code,409,result.json)
        self.db.run('UPDATE conversations SET sync_complete=1 WHERE id=?',(other['id'],))
        result=self.call(url,data={'channel':'Webchat','scope':'customer'})
        self.assertEqual(result.status_code,200,result.json)
        self.assertEqual(result.json['values']['test_identity_allowlist'],['user:user-2'])
        self.assertFalse(result.json['values']['auto_reply_enabled'])
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE state IN ('queued','pending')")['n'],0)
        self.assertEqual(self.webhook(self.event(self.message('old-user-new-message'))).json['status'],'ignored')
        self.assertEqual(self.webhook(self.event(self.message('selected-user-new-message',conversation_id='conv-second',user_id='user-2'))).json['status'],'accepted')
        revision=self.svc.settings.get()[1]
        self.assertEqual(self.call(url,data={'channel':'Webchat','scope':'customer'}).status_code,200)
        self.assertEqual(self.svc.settings.get()[1],revision)
        self.assertEqual(self.call(url,data={'channel':'Webchat','scope':'nickname'}).status_code,400)
        self.svc.settings.save({'test_identity_allowlist':[]})
        self.assertEqual(self.webhook(self.event(self.message('paused-message',conversation_id='conv-second',user_id='user-2'))).json['status'],'ignored')

    def test_background_scope_rechecked_and_manual_import_still_works(self):
        self.svc.settings.save({'test_identity_allowlist':[]})
        job=self.svc.enqueue('sync',self.c,origin='webhook')
        with patch('lsp.providers.conversation') as read:
            self.svc.drain()
            read.assert_not_called()
        self.assertEqual(self.svc.public_job(job)['state'],'cancelled')
        with patch('lsp.providers.conversation',return_value={'conversation_id':'conv-manual'}),patch('lsp.providers.history_pages',return_value=iter([])):
            result=self.svc.import_conversation('conv-manual')
            self.svc.drain()
        self.assertEqual(self.svc.public_job(result['job_id'])['state'],'completed')
        self.assertEqual(self.svc.settings.get()[0]['test_identity_allowlist'],[])

    def test_platform_credential_change_clears_test_scope(self):
        self.svc.settings.save({'freshchat_token':'other-platform-credential'})
        self.assertEqual(self.svc.settings.get()[0]['test_identity_allowlist'],[])
        self.assertEqual(self.webhook().json['status'],'ignored')
        self.assertEqual(self.db.one('SELECT count(*) n FROM events')['n'],0)

    def test_signature_raw_bytes_missing_and_tamper(self):
        self.assertEqual(self.webhook(signature=False).status_code,401)
        self.assertEqual(self.webhook(mutate=True).status_code,401)
        self.assertEqual(self.webhook().status_code,200)
        self.assertEqual(self.db.one('SELECT version FROM events')['version'],'test-1')

    def test_rsa_public_key_formats_save_and_verify_webhooks(self):
        key=self.rsa.public_key()
        pkcs1=key.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.PKCS1).decode()
        # Some copied public keys have an RSA PEM label around SubjectPublicKeyInfo DER.
        rsa_label=self.pem.replace('BEGIN PUBLIC KEY','BEGIN RSA PUBLIC KEY').replace('END PUBLIC KEY','END RSA PUBLIC KEY')
        der=key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
        formats=[self.pem,pkcs1,rsa_label,'\r\n '+rsa_label.replace('\n','\r\n')+' \r\n',
                 base64.b64encode(der).decode(),base64.encodebytes(der).decode(),
                 base64.b64encode(key.public_bytes(serialization.Encoding.DER,serialization.PublicFormat.PKCS1)).decode()]
        for i,value in enumerate(formats):
            with self.subTest(format=i):
                saved=self.call('/api/settings','PUT',{'freshchat_public_key':value})
                self.assertEqual(saved.status_code,200,saved.json)
                self.assertEqual(security.public_key(value).public_numbers(),key.public_numbers())
                self.assertEqual(self.webhook(signature=False).json['error'],'invalid_signature')
                self.assertEqual(self.webhook(mutate=True).json['error'],'invalid_signature')
                accepted=self.webhook(self.event(self.message('key-format-'+str(i))))
                self.assertEqual(accepted.status_code,200,accepted.json)
                self.assertEqual(accepted.json['status'],'accepted')

    def test_public_key_rejects_invalid_private_non_rsa_and_weak_keys(self):
        private=self.rsa.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
        weak=rsa.generate_private_key(public_exponent=65537,key_size=1024).public_key()
        non_rsa=ec.generate_private_key(ec.SECP256R1()).public_key()
        invalid=['not a key','-----BEGIN RSA PUBLIC KEY-----\ninvalid!\n-----END RSA PUBLIC KEY-----',
                 self.pem.replace('END PUBLIC KEY','END RSA PUBLIC KEY'),private]
        invalid += [key.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
                    for key in (weak,non_rsa)]
        previous=self.svc.settings.get()
        for i,value in enumerate(invalid):
            with self.subTest(format=i):
                saved=self.call('/api/settings','PUT',{'freshchat_public_key':value})
                self.assertEqual(saved.status_code,400,saved.json)
                self.assertEqual(saved.json['error'],'invalid_public_key')
                self.assertEqual(self.svc.settings.get(),previous)

    def test_dedup_event_and_task(self):
        self.enable()
        self.assertEqual(self.webhook().json['status'],'accepted')
        self.assertEqual(self.webhook().json['status'],'duplicate')
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n'],1)
        self.assertEqual(self.db.one('SELECT count(*) n FROM messages')['n'],1)

    def test_webhook_atomic_persist_failure(self):
        original=self.svc.enqueue
        with patch.object(self.svc,'enqueue',side_effect=RuntimeError('synthetic db failure')):
            self.assertEqual(self.webhook().status_code,500)
        self.assertEqual(self.db.one('SELECT count(*) n FROM messages')['n'],0)
        self.assertEqual(self.db.one('SELECT count(*) n FROM events')['n'],0)
        self.assertEqual(self.webhook().status_code,200)

    def test_roles_self_echo_private_system_do_not_generate(self):
        self.enable()
        for msg in [self.message('echo','agent',actor_id='agent-self'),self.message('note','user',private=True),self.message('sys','system')]:
            self.assertEqual(self.webhook(self.event(msg)).status_code,200)
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n'],0)
        self.assertEqual(self.svc.conv(self.c['id'])['mode'],'auto')
        self.webhook(self.event(self.message('human','agent')))
        self.assertEqual(self.svc.conv(self.c['id'])['mode'],'manual')

    def test_enabling_boundary_does_not_backfill_history(self):
        self.add_history(self.message(created='2020-01-01T00:00:00Z'));self.enable()
        self.webhook(self.event(self.message('older',created='2020-01-02T00:00:00Z')))
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n'],0)
        self.webhook(self.event(self.message('new')))
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n'],1)

    def test_debounce_and_append_cancel_old_plan(self):
        self.enable();self.webhook()
        first=self.db.one("SELECT id FROM jobs WHERE kind='generate'")['id']
        self.webhook(self.event(self.message('m2',created='2030-01-01T00:00:02Z')))
        self.assertEqual(self.svc.public_job(first)['state'],'cancelled')
        jobs=self.db.all("SELECT * FROM jobs WHERE kind='generate' AND state='queued'")
        self.assertEqual(len(jobs),1);self.assertEqual(jobs[0]['trigger_id'],'m2');self.assertGreater(jobs[0]['due'],time.time())

    def test_unknown_source_allowlist_and_identity_isolation(self):
        self.enable()
        response=self.webhook(self.event(self.message(message_source='unmapped',conversation_id='conv-2')))
        self.assertEqual(response.status_code,200)
        c=self.svc.add_conversation('conv-2')
        self.assertEqual(c['channel'],'unknown')
        with self.assertRaises(security.Problem):self.svc.manual_send(c['id'],[self.text()])
        bad=self.webhook(self.event(self.message('bad',user_id='other-user')))
        self.assertEqual(bad.status_code,409)
        self.assertEqual(self.db.one('SELECT count(*) n FROM messages WHERE platform_id=?',('bad',))['n'],0)

    def test_full_history_pagination_over_50_and_dedup(self):
        rows=[self.message('m%03d'%i,created='2024-01-01T00:%02d:%02dZ'%(i//60,i%60)) for i in range(61)]
        with patch('lsp.providers.freshchat',side_effect=[{'messages':rows[:50]},{'messages':rows[49:]},{'messages':[]}]) as client,patch('lsp.providers.conversation',return_value={'conversation_id':'conv-1'}):
            result=self.svc.sync(self.c['id']);self.assertEqual(result['new_messages'],61)
            self.assertIn('page=3',client.call_args_list[-1].args[1])
        self.assertEqual(self.db.one('SELECT count(*) n FROM messages')['n'],61)
        self.assertTrue(self.svc.conv(self.c['id'])['sync_complete'])
        with patch('lsp.providers.freshchat',side_effect=[{'messages':[rows[-1]]},{'messages':[]}]) as client,patch('lsp.providers.conversation',return_value={}):
            self.assertEqual(self.svc.sync(self.c['id'])['new_messages'],0)
            self.assertIn('from_time=',client.call_args_list[0].args[1])

    def test_history_failure_never_claims_complete(self):
        with patch('lsp.providers.conversation',side_effect=security.Problem('upstream_rejected','供应商 HTTP 403',502)):
            with self.assertRaises(security.Problem):self.svc.sync(self.c['id'])
        c=self.svc.conv(self.c['id']);self.assertFalse(c['sync_complete']);self.assertIn('403',c['sync_error'])

    def test_media_inbound_and_private_note_stay_distinct(self):
        msg=self.message(message_parts=[{'image':{'url':'https://media.example/a.png'}},{'video':{'url':'https://media.example/v.mp4','content_type':'video/mp4'}},{'file':{'name':'scheme.pdf','file_size_in_bytes':100,'content_type':'application/pdf','url':'https://media.example/a.pdf'}}])
        self.webhook(self.event(msg));self.add_history(self.message('private',private=True))
        response=self.call('/api/conversations/%s/messages'%self.c['id'],'GET').json
        media=next(m for m in response['messages'] if m['platform_id']=='m1')
        self.assertEqual([p['type'] for p in media['parts']],['image','video','file'])
        self.assertTrue(media['parts'][0]['url'].startswith('/api/conversations/'))
        self.assertEqual(next(m for m in response['messages'] if m['platform_id']=='private')['role'],'private')

    def test_manual_takeover_and_partial_send_sequence(self):
        self.enable();self.webhook()
        result=self.svc.manual_send(self.c['id'],[self.text('one'),self.text('two'),self.text('three')])
        self.assertEqual(self.svc.conv(self.c['id'])['mode'],'manual')
        self.db.run("UPDATE jobs SET state='cancelled' WHERE kind='sync'")
        with patch('lsp.providers.send_message',side_effect=[{'id':'out-1'},security.Problem('upstream_rejected','HTTP 400',502)]) as send:
            self.svc.drain();self.assertEqual(send.call_count,2)
        jobs=[self.svc.public_job(i) for i in result['job_ids']]
        self.assertEqual([j['state'] for j in jobs],['accepted','failed','paused'])
        self.svc.drain();self.assertEqual(jobs[0]['attempts'],1)
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE origin='ai' AND state!='cancelled'")['n'],0)

    def test_takeover_during_inflight_truthful(self):
        result=self.svc.manual_send(self.c['id'],[self.text()])
        def send(*args):
            response=self.svc.set_mode(self.c['id'],'manual');self.assertEqual(response['inflight'],1)
            return {'id':'inflight-accepted'}
        with patch('lsp.providers.send_message',side_effect=send):self.svc.drain()
        self.assertEqual(self.svc.public_job(result['job_ids'][0])['state'],'accepted')

    def test_network_unknown_restart_does_not_resend(self):
        result=self.svc.manual_send(self.c['id'],[self.text(),self.text()])
        with patch('lsp.providers.send_message',side_effect=security.Problem('network_unknown','timeout',502)) as send:
            self.svc.drain();self.svc.drain();self.assertEqual(send.call_count,1)
        self.assertEqual(self.svc.public_job(result['job_ids'][0])['state'],'unknown')
        job=self.svc.enqueue('send',self.c,self.text())
        self.db.run("UPDATE jobs SET state='sending' WHERE id=?",(job,))
        restarted=Service(self.db,self.config['FILE_DIRECTORY'])
        self.assertEqual(restarted.public_job(job)['state'],'unknown')
        safe=restarted.enqueue('sync',self.c);self.db.run("UPDATE jobs SET state='generating' WHERE id=?",(safe,))
        restarted.recover_after_restart();self.assertEqual(restarted.public_job(safe)['state'],'queued')

    def test_safe_429_has_delayed_bounded_retry(self):
        result=self.svc.manual_send(self.c['id'],[self.text()]);job=result['job_ids'][0]
        with patch('lsp.providers.send_message',side_effect=security.Problem('rate_limited','429',429,10)):
            self.svc.drain();j=self.svc.public_job(job);self.assertEqual(j['state'],'pending');self.assertGreater(j['due'],time.time()+5)
            for _ in range(2):self.db.run('UPDATE jobs SET due=0 WHERE id=?',(job,));self.svc.drain()
        self.assertEqual(self.svc.public_job(job)['state'],'failed');self.assertEqual(self.svc.public_job(job)['attempts'],3)

    def test_invalid_plans_unknown_asset_empty_and_extra_fields(self):
        invalid=[{'messages':[self.text('')],'needs_human':False,'ticket_reason':None},
                 {'messages':[self.text()]*4,'needs_human':False,'ticket_reason':None},
                 {'messages':[{'type':'image','text':None,'asset_id':'unknown'}],'needs_human':False,'ticket_reason':None},
                 {'messages':[dict(self.text(),url='https://evil.example')],'needs_human':False,'ticket_reason':None},
                 {'messages':[],'needs_human':'true','ticket_reason':None}]
        for plan in invalid:
            with self.subTest(plan=plan),self.assertRaises(security.Problem):self.svc.validate_plan(plan,self.c)

    def test_responses_iterate_refusal_incomplete_and_invalid_json(self):
        self.assertEqual(providers.parse_response(self.model_response())['messages'][0]['type'],'text')
        cases=[self.model_response(status='incomplete'),{'status':'completed','output':[{'type':'message','content':[{'type':'refusal'}]}]},
               {'status':'completed','output':[]},{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'bad json'}]}]},
               {'status':'completed','output':None},{'status':'completed','output':['not-an-object']}]
        for data in cases:
            with self.subTest(data=data),self.assertRaises(security.Problem):providers.parse_response(data)

    def test_context_private_filter_store_false_and_real_preview_no_send(self):
        self.add_history();self.add_history(self.message('secret',private=True,message_parts=[{'text':{'content':'internal-note-secret'}}]))
        job=self.svc.preview(self.c['id'])['job_id']
        with patch.object(self.svc,'sync',return_value={}),patch('lsp.providers.openai',return_value=(self.model_response(),'req_test')) as model,patch('lsp.providers.send_message') as send:
            self.svc.drain();send.assert_not_called()
        payload=model.call_args.args[1]
        self.assertIs(payload['store'],False);self.assertNotIn('previous_response_id',payload)
        self.assertNotIn('internal-note-secret',payload['input']);self.assertNotIn('synthetic-platform-token',payload['input'])
        self.assertEqual(payload['text']['format']['type'],'json_schema')
        self.assertEqual(self.svc.public_job(job)['result']['request_id'],'req_test')
        self.assertEqual(self.svc.public_job(job)['state'],'completed')

    def test_context_truncates_and_preserves_latest(self):
        s,_=self.svc.settings.get();s['context_token_budget']=2200
        rows=[{'id':str(i),'actor':'user','text':'x'*300} for i in range(20)]
        payload,ctx=providers.response_payload(s,rows,[])
        self.assertTrue(ctx['truncated']);self.assertEqual(ctx['last'],'19');self.assertLess(ctx['count'],20)
        self.assertGreater(len(json.loads(payload['input'])['history']),0)

    def test_config_change_disables_and_invalidates_and_hides_old_tenant(self):
        self.enable();self.add_history();old_revision=self.svc.settings.get()[1]
        result=self.svc.settings.save({'openai_model':'other-model'})
        self.assertFalse(result['values']['auto_reply_enabled']);self.assertGreater(result['revision'],old_revision)
        self.assertFalse(self.svc.settings.passed('*','openai'))
        self.svc.settings.save({'freshchat_token':'other-tenant-token'})
        self.assertEqual(self.call('/api/conversations','GET').json['conversations'],[])
        self.assertEqual(self.call('/api/conversations/%s/messages'%self.c['id'],'GET').status_code,404)

    def test_custom_openai_address_persistence_defaults_and_invalidation(self):
        self.enable()
        cases={
            ' https://gateway.example/v1/ ':'https://gateway.example/v1',
            'https://gateway.example/proxy/v1/responses/':'https://gateway.example/proxy/v1/responses',
            'https://gateway.example/responses':'https://gateway.example/responses',
            'https://gateway.example':'https://gateway.example/v1',
            '  ':DEFAULTS['openai_base_url'],
        }
        for address,expected in cases.items():
            with self.subTest(address=address):
                result=self.call('/api/settings','PUT',{'openai_base_url':address})
                self.assertEqual(result.status_code,200,result.json)
                self.assertEqual(result.json['values']['openai_base_url'],expected)
                self.assertEqual(Settings(self.db).get()[0]['openai_base_url'],expected)
                self.assertFalse(result.json['values']['auto_reply_enabled'])
                self.assertFalse(self.svc.settings.passed('*','openai'))
                self.assertEqual(result.json['values']['openai_api_key'],'')
                self.assertEqual(self.svc.settings.get()[0]['openai_api_key'],'synthetic-openai-key')
                revision=result.json['revision']
                self.assertEqual(self.call('/api/settings','PUT',{'openai_base_url':expected}).json['revision'],revision)

    def test_custom_openai_check_uses_configured_endpoint_and_responses_contract(self):
        for base,endpoint in [('https://gateway.example/proxy/v1','https://gateway.example/proxy/v1/responses'),
                              ('https://gateway.example/v1/responses','https://gateway.example/v1/responses'),
                              ('https://gateway.example/responses','https://gateway.example/responses'),
                              (DEFAULTS['openai_base_url'],'https://api.openai.com/v1/responses')]:
            self.svc.settings.save({'openai_base_url':base})
            with patch('lsp.security.json_request',return_value=(self.model_response(),{'x-request-id':'local-custom-url-check'})) as call:
                result=self.call('/api/checks',data={'kind':'openai'})
            self.assertEqual(result.status_code,200,result.json)
            args=call.call_args.args
            self.assertEqual(args[0:2],(endpoint,'POST'))
            self.assertEqual(args[2]['Authorization'],'Bearer synthetic-openai-key')
            self.assertIs(args[3]['store'],False)
            self.assertTrue(args[3]['text']['format']['strict'])
            self.assertEqual(result.json['request_id'],'local-custom-url-check')
            self.assertNotIn('synthetic-openai-key',json.dumps(result.json))

    def test_only_openai_uses_https_proxy_environment(self):
        s,_=self.svc.settings.get()
        for lower,upper,expected in [('http://127.0.0.1:7897','http://127.0.0.1:9999','http://127.0.0.1:7897'),
                                     ('','http://127.0.0.1:7897','http://127.0.0.1:7897'),('','',None)]:
            with self.subTest(lower=lower,upper=upper),patch.dict(os.environ,{'https_proxy':lower,'HTTPS_PROXY':upper,
                    'http_proxy':'http://127.0.0.1:7897','all_proxy':'socks5://127.0.0.1:7897'}), \
                    patch('lsp.security.json_request',return_value=({},{})) as request:
                providers.openai(s,{'store':False})
                self.assertEqual(request.call_args.kwargs.get('proxy'),expected)
                providers.freshchat(s,'/agents')
                self.assertIsNone(request.call_args.kwargs.get('proxy'))
                providers.freshdesk(s,'/tickets')
                self.assertIsNone(request.call_args.kwargs.get('proxy'))

    def test_openai_proxy_resolves_official_hostname_and_preserves_tls_and_host(self):
        tunnel=Mock()
        tunnel.makefile.return_value=io.BytesIO(b'HTTP/1.1 200 Connection established\r\n\r\n')
        tls=Mock()
        tls.makefile.return_value=io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
        context=ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode,ssl.CERT_REQUIRED)
        with patch('socket.getaddrinfo',return_value=[(10,1,6,'',('2001::6817:7dbd',443,0,0))]) as resolve, \
             patch('socket.create_connection',return_value=tunnel) as connect, \
             patch('ssl.create_default_context',return_value=context), \
             patch.object(context,'wrap_socket',return_value=tls) as wrap:
            data,_=security.json_request('https://api.openai.com/v1/responses','POST',
                {'Authorization':'Bearer synthetic-key'},{'store':False},proxy='http://127.0.0.1:7897')
        self.assertEqual(data,{})
        resolve.assert_not_called()
        self.assertEqual(connect.call_args.args[0],('127.0.0.1',7897))
        wrap.assert_called_once_with(tunnel,server_hostname='api.openai.com')
        connect_bytes=b''.join(call.args[0] for call in tunnel.sendall.call_args_list)
        self.assertIn(b'CONNECT api.openai.com:443 HTTP/',connect_bytes)
        self.assertNotIn(b'synthetic-key',connect_bytes)
        encrypted_bytes=b''.join(call.args[0] for call in tls.sendall.call_args_list)
        self.assertIn(b'Host: api.openai.com\r\n',encrypted_bytes)
        self.assertIn(b'Authorization: Bearer synthetic-key\r\n',encrypted_bytes)
        tls.close.assert_called_once()

    def test_proxy_dns_exception_is_only_for_official_openai_through_local_proxy(self):
        for host,proxy in [('api.openai.com',None), ('api.openai.com.evil.example','http://127.0.0.1:7897'),
                           ('gateway.example','http://127.0.0.1:7897')]:
            with self.subTest(host=host,proxy=proxy), \
                 patch('socket.getaddrinfo',return_value=[(2,1,6,'',('198.18.0.1',443))]), \
                 patch('socket.create_connection') as connect:
                with self.assertRaises(security.Problem) as error:
                    security.request('https://'+host+'/v1/responses',proxy=proxy)
                self.assertEqual(error.exception.code,'private_host')
                connect.assert_not_called()
        context=ssl.create_default_context()
        tunnel=Mock()
        with patch('lsp.security.public_addresses',return_value=['8.8.8.8']) as resolve, \
             patch('http.client.HTTPConnection',return_value=tunnel), \
             patch.object(context,'wrap_socket') as wrap:
            conn=security.PinnedHTTPS('gateway.example',context=context,proxy='http://127.0.0.1:7897')
            conn.connect()
            conn.close()
        resolve.assert_called_once_with('gateway.example')
        tunnel.set_tunnel.assert_called_once_with('8.8.8.8',443)
        self.assertEqual(wrap.call_args.kwargs['server_hostname'],'gateway.example')

    def test_openai_proxy_rejects_unsafe_config_and_private_target(self):
        for proxy in ('socks5://127.0.0.1:7897','http://evil.example:7897','http://192.168.1.1:7897',
                      'http://user:secret@127.0.0.1:7897','http://127.0.0.1:bad',
                      'http://127.0.0.1:0','http://127.0.0.1:7897/path','http://127.0.0.1:7897?key=secret'):
            with self.subTest(proxy=proxy),patch('socket.create_connection') as connect:
                with self.assertRaises(security.Problem) as error:
                    security.request('https://api.openai.com/v1/responses',proxy=proxy)
                self.assertEqual(error.exception.code,'invalid_proxy')
                self.assertNotIn('secret',error.exception.message)
                connect.assert_not_called()
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]), \
             patch('socket.create_connection') as connect:
            with self.assertRaises(security.Problem) as error:
                security.request('https://gateway.example/responses',proxy='http://127.0.0.1:7897')
            self.assertEqual(error.exception.code,'private_host')
            connect.assert_not_called()

    def test_openai_proxy_failure_never_falls_back_to_direct(self):
        with patch('lsp.security.public_addresses',return_value=['8.8.8.8']), \
             patch('socket.create_connection',side_effect=ConnectionRefusedError) as connect:
            with self.assertRaises(security.Problem) as error:
                security.request('https://api.openai.com/v1/responses','POST',proxy='http://127.0.0.1:7897')
            self.assertEqual(error.exception.code,'proxy_unavailable')
            self.assertEqual(connect.call_count,1)
            self.assertEqual(connect.call_args.args[0],('127.0.0.1',7897))
        tunnel=Mock()
        tunnel.makefile.return_value=io.BytesIO(b'HTTP/1.1 200 Connection established\r\n\r\n')
        context=ssl.create_default_context()
        with patch('lsp.security.public_addresses',return_value=['8.8.8.8']), \
             patch('socket.create_connection',return_value=tunnel) as connect, \
             patch('ssl.create_default_context',return_value=context), \
             patch.object(context,'wrap_socket',side_effect=ssl.SSLCertVerificationError):
            with self.assertRaises(security.Problem):
                security.request('https://api.openai.com/v1/responses','POST',proxy='http://127.0.0.1:7897')
            self.assertEqual(connect.call_count,1)
            tunnel.close.assert_called_once()

    def test_custom_openai_rejects_unsafe_urls_private_dns_and_redirects(self):
        for url in ('http://gateway.example/v1','https://127.0.0.1/v1','https://169.254.169.254/v1',
                    'https://[::1]/v1','https://localhost/v1','https://host.internal/v1',
                    'https://key:secret@gateway.example/v1','https://gateway.example/v1?key=secret',
                    'https://gateway.example/v1#secret','https://gateway.example:8443/v1',
                    'https://gateway.example/v1/chat/completions'):
            with self.subTest(url=url):
                result=self.call('/api/settings','PUT',{'openai_base_url':url})
                self.assertEqual(result.status_code,400,result.json)
                self.assertNotIn('secret',json.dumps(result.json))
        self.assertEqual(self.call('/api/settings','PUT',{'platform_api_base_url':'https://gateway.example'}).status_code,400)
        self.svc.settings.save({'openai_base_url':'https://gateway.example/v1'})
        s,_=self.svc.settings.get()
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),patch('socket.create_connection') as connect:
            with self.assertRaises(security.Problem) as raised:providers.openai(s,{'store':False})
            self.assertEqual(raised.exception.code,'private_host')
            connect.assert_not_called()
        with patch('lsp.security.PinnedHTTPS') as connection:
            response=connection.return_value.getresponse.return_value
            response.status=302
            response.getheaders.return_value=[('location','https://elsewhere.example/responses')]
            with self.assertRaises(security.Problem) as raised:providers.openai(s,{'store':False})
            self.assertEqual(raised.exception.code,'redirect_denied')
            self.assertEqual(connection.call_count,1)
            connection.return_value.close.assert_called_once()

    def test_customer_append_during_generation_never_sends_stale(self):
        self.enable();self.webhook();self.db.run("UPDATE jobs SET state='cancelled' WHERE kind='sync'");self.db.run('UPDATE jobs SET due=0')
        def response(*args):
            self.webhook(self.event(self.message('m2',created='2030-01-01T00:00:02Z')))
            return self.model_response(),'req_stale'
        with patch.object(self.svc,'sync',return_value={}),patch('lsp.providers.openai',side_effect=response),patch('lsp.providers.send_message') as send:
            self.svc.drain(1);send.assert_not_called()
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='send'")['n'],0)

    def test_ai_generation_and_send_with_usage_evidence(self):
        self.enable();self.webhook();self.db.run("UPDATE jobs SET state='cancelled' WHERE kind='sync'");self.db.run('UPDATE jobs SET due=0')
        with patch.object(self.svc,'sync',return_value={}),patch('lsp.providers.openai',return_value=(self.model_response(),'req_auto')),patch('lsp.providers.send_message',return_value={'id':'out-auto'}) as send:
            self.svc.drain();self.assertEqual(send.call_count,1)
        sent=self.db.one("SELECT * FROM jobs WHERE kind='send'");self.assertEqual(sent['origin'],'ai');self.assertEqual(sent['state'],'accepted')
        self.assertEqual(self.db.one('SELECT tokens FROM usage')['tokens'],150)

    def test_uncertain_ai_plan_still_sends_honest_reply(self):
        self.enable();self.webhook();self.db.run("UPDATE jobs SET state='cancelled' WHERE kind='sync'");self.db.run('UPDATE jobs SET due=0')
        plan={'messages':[],'needs_human':True,'ticket_reason':None}
        with patch.object(self.svc,'sync',return_value={}),patch('lsp.providers.openai',return_value=(self.model_response(plan),'req_uncertain')),patch('lsp.providers.send_message',return_value={'id':'out-uncertain'}) as send:
            self.svc.drain();self.assertEqual(send.call_count,1)
        sent=self.db.one("SELECT * FROM jobs WHERE kind='send'")
        self.assertIn('無法確認',self.db.unseal(sent['payload'])['text'])
        self.assertEqual(self.svc.conv(self.c['id'])['mode'],'auto')

    def test_human_suggestion_keeps_model_reply_and_ai_enabled(self):
        self.enable();self.webhook();self.db.run("UPDATE jobs SET state='cancelled' WHERE kind='sync'");self.db.run('UPDATE jobs SET due=0')
        plan={'messages':[self.text('我无法确认，请补充停车场名称。')],'needs_human':True,'ticket_reason':None}
        with patch.object(self.svc,'sync',return_value={}),patch('lsp.providers.openai',return_value=(self.model_response(plan),'req_suggestion')) as model,patch('lsp.providers.send_message',return_value={'id':'out-suggestion'}) as send:
            self.svc.drain();send.assert_called_once()
        self.assertEqual(send.call_args.args[2],plan['messages'][0])
        self.assertNotIn('tools',model.call_args.args[1])
        self.assertEqual(self.svc.conv(self.c['id'])['mode'],'auto')

    def test_ticket_dedup_requester_auth_payload_and_unknown(self):
        first=self.svc.create_ticket(self.c['id'],'需要人工跟进');second=self.svc.create_ticket(self.c['id'],'连续追问')
        self.assertEqual(first['job_id'],second['job_id'])
        with patch('lsp.providers.freshdesk',return_value={'id':100,'status':2}) as desk:self.svc.drain()
        payload=desk.call_args.args[3];self.assertEqual(payload['requester_id'],42);self.assertNotIn('user-1',str(payload['requester_id']))
        self.assertIn('conv-1',payload['description']);self.assertEqual(self.svc.create_ticket(self.c['id'],'same')['ticket_id'],100)
        next_ticket=self.svc.create_ticket(self.c['id'],'新事项',new_matter=True)
        with patch('lsp.providers.freshdesk',side_effect=security.Problem('network_unknown','timeout',502)):self.svc.drain()
        self.assertEqual(self.svc.public_job(next_ticket['job_id'])['state'],'unknown')
        self.assertEqual(self.svc.create_ticket(self.c['id'],'same')['state'],'unknown')
        with self.assertRaises(security.Problem):self.svc.create_ticket(self.c['id'],'another',new_matter=True)

    def test_freshdesk_basic_auth_and_file_part_mapping(self):
        s,_=self.svc.settings.get()
        with patch('lsp.security.json_request',return_value=({'id':1},{})) as req:providers.freshdesk(s,'/tickets','POST',{'requester_id':42})
        auth=req.call_args.args[2]['Authorization'];self.assertEqual(base64.b64decode(auth.split()[1]).decode(),'synthetic-desk-key:X')
        asset={'ref':{'file_hash':'hash123'},'filename':'plan.pdf','mime':'application/pdf','size':55}
        with patch('lsp.providers.freshchat',return_value={'id':'out'}) as fc:providers.send_message(s,'conv-1',{'type':'file'},asset)
        part=fc.call_args.args[3]['message_parts'][0]['file']
        self.assertEqual(part['fileHash'],'hash123');self.assertEqual(part['fileSource'],'FRESHCHAT');self.assertNotIn('file_hash',part)

    def test_asset_scan_state_and_immutable_versions(self):
        metadata={'asset_id':'parking_pdf','name':'方案','purpose':'已审核测试方案','channels':['Webchat']}
        a=self.svc.save_asset(metadata,b'%PDF-1.7\nsynthetic content\n%%EOF','plan.pdf','application/pdf')
        b=self.svc.save_asset(metadata,b'%PDF-1.7\nnew synthetic content\n%%EOF','plan.pdf','application/pdf')
        self.assertEqual(b['version'],2);self.assertNotEqual(a['id'],b['id'])
        with patch('lsp.security.request',return_value=(json.dumps({'file_hash':'hash','file_security_status':'AV_PENDING'}).encode(),{})):
            uploaded=self.svc.upload_asset(a['id'])
        self.assertEqual(uploaded['state'],'scanning')
        with self.assertRaises(security.Problem):self.svc.manual_send(self.c['id'],[{'type':'file','text':None,'asset_id':a['id']}])
        with patch('lsp.security.request',return_value=(json.dumps({'file_hash':'hash','file_security_status':'SAFE_FILE'}).encode(),{})):
            uploaded=self.svc.upload_asset(a['id'])
        self.assertEqual(uploaded['state'],'sendable')
        self.svc.manual_send(self.c['id'],[{'type':'file','text':None,'asset_id':a['id']}])
        self.assertEqual(self.db.unseal(self.db.one("SELECT payload FROM jobs WHERE kind='send'")['payload'])['asset_id'],a['id'])

    def test_chat_attachment_upload_scope_and_model_exclusion(self):
        with patch('lsp.providers.upload',return_value=({'file_hash':'local-hash'},'sendable')):
            response=self.client.post('/api/conversations/%s/attachments'%self.c['id'],data={'type':'file','file':(io.BytesIO(b'%PDF-1.7\nlocal test'),'plan.pdf')},headers={'X-CSRF-Token':self.csrf})
        self.assertEqual(response.status_code,201,response.json)
        asset=response.json;self.assertEqual(asset['state'],'sendable')
        self.assertEqual(self.db.one('SELECT conversation FROM assets WHERE id=?',(asset['id'],))['conversation'],self.c['id'])
        self.assertEqual(self.client.get('/api/assets').json['assets'],[])
        message={'type':'file','text':None,'asset_id':asset['id']}
        self.svc.manual_send(self.c['id'],[message])
        other=self.svc.add_conversation('conv-other','user-1','web')
        for c,automatic in [(other,False),(self.c,True)]:
            with self.assertRaises(security.Problem) as error:self.svc.validate_plan({'messages':[message],'needs_human':False,'ticket_reason':None},c,automatic)
            self.assertEqual(error.exception.code,'attachment_scope')
        self.enable();self.db.run('UPDATE conversations SET sync_complete=1 WHERE id=?',(self.c['id'],));self.svc.set_mode(self.c['id'],'auto');self.webhook();self.db.run("UPDATE jobs SET state='cancelled' WHERE kind='sync'");self.db.run('UPDATE jobs SET due=0')
        with patch.object(self.svc,'sync',return_value={}),patch('lsp.providers.openai',return_value=(self.model_response(),'req_attachment')) as model,patch('lsp.providers.send_message',return_value={'id':'reply'}):self.svc.drain()
        self.assertEqual(json.loads(model.call_args.args[1]['input'])['assets'],[])

    def test_chat_attachment_validation_auth_and_scan(self):
        url='/api/conversations/%s/attachments'%self.c['id']
        self.assertEqual(self.app.test_client().post(url).status_code,401)
        self.assertEqual(self.client.post(url).status_code,403)
        self.assertEqual(self.call(url).status_code,400)
        response=self.client.post(url,data={'type':'image','file':(io.BytesIO(b'%PDF-1.7\nlocal test'),'wrong.pdf')},headers={'X-CSRF-Token':self.csrf})
        self.assertEqual(response.status_code,400);self.assertEqual(response.json['error'],'attachment_type')
        with patch('lsp.providers.upload',return_value=({'file_hash':'local-hash'},'scanning')):
            asset=self.svc.save_attachment(self.c['id'],b'%PDF-1.7\nlocal test','scan.pdf','application/pdf','file')
        self.assertEqual(asset['state'],'scanning')
        with self.assertRaises(security.Problem):self.svc.manual_send(self.c['id'],[{'type':'file','text':None,'asset_id':asset['id']}])
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='send'")['n'],0)

    def test_chat_image_and_video_upload_without_library_form(self):
        with patch('lsp.providers.upload',return_value=({'url':'https://media.example/image.png'},'sendable')):
            image=self.svc.save_attachment(self.c['id'],b'\x89PNG\r\n\x1a\n'+b'x'*30,'map.png','image/png','image')
        video=self.svc.save_attachment(self.c['id'],b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42','guide.mp4','video/mp4','video')
        self.assertEqual(image['kind'],'image');self.assertEqual(video['kind'],'video')
        jobs=self.svc.manual_send(self.c['id'],[{'type':a['kind'],'text':None,'asset_id':a['id']} for a in (image,video)])
        self.assertEqual(len(jobs['job_ids']),2)

    def test_file_validation_ssrf_dns_rebinding_and_host_allowlist(self):
        for url in ['http://media.example/x','https://127.0.0.1/x','https://169.254.169.254/x','https://[::1]/x','https://user:pw@media.example/x','https://localhost/x']:
            with self.subTest(url=url),self.assertRaises(security.Problem):security.url_parts(url)
        with self.assertRaises(security.Problem):security.url_parts('https://evil.example/x',['media.example'])
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]),self.assertRaises(security.Problem):security.public_addresses('media.example')
        with self.assertRaises(security.Problem):security.detect_file('image.png',b'<html>evil</html>','image/png')
        with self.assertRaises(security.Problem):security.detect_file('image.html',b'\x89PNG\r\n\x1a\nxxxx','text/html')
        with self.assertRaises(security.Problem):self.svc.settings.save({'media_size_limits':{'image':99999999,'video':1,'file':1}})

    def test_http_redirect_target_revalidated(self):
        class FakeResponse:
            status=302
            def getheaders(self):return [('location','https://127.0.0.1/private')]
        class FakeConnection:
            def __init__(self,*a,**k):pass
            def request(self,*a,**k):pass
            def getresponse(self):return FakeResponse()
            def close(self):pass
        with patch('lsp.security.PinnedHTTPS',FakeConnection),self.assertRaises(security.Problem):
            security.request('https://media.example/start',hosts=['media.example'],redirects=3)

    def test_scheduler_auth_bodies_dry_run_execution_no_sensitive_data(self):
        route='/api/internal/jobs/drain'
        self.assertEqual(self.client.post(route,json={}).status_code,401)
        self.assertEqual(self.client.post(route,json={},headers={'X-Scheduler-Token':'wrong'}).status_code,401)
        headers={'X-Scheduler-Token':'s'*40}
        for data in [{'limit':101},{'limit':True},{'tasks':['arbitrary']},{'dry_run':False},{'raw':True},{'tasks':[]}]:
            self.assertEqual(self.client.post(route,json=data,headers=headers).status_code,400)
        self.svc.enqueue('sync',self.c)
        response=self.client.post(route,json={},headers=headers);self.assertEqual(response.status_code,200);self.assertEqual(response.json['processed'],0)
        with patch.object(self.svc,'sync',return_value={'complete':True}):
            response=self.client.post(route,json={'dry_run':False,'allow_external_effects':True},headers=headers)
        self.assertEqual(response.json['processed'],1)
        self.assertEqual(set(response.json),{'status','request_id','dry_run','selected','processed','failed','skipped_unknown'})
        self.assertNotIn('user-1',response.text);self.assertNotIn('synthetic',response.text)
        self.svc.settings.save({'clear_secrets':['scheduler_token']})
        self.assertEqual(self.client.post(route,json={},headers=headers).status_code,503)

    def test_scheduler_lock_and_unknown_skip(self):
        self.svc.enqueue('sync',self.c);job=self.svc.enqueue('send',self.c,self.text());self.db.run("UPDATE jobs SET state='unknown' WHERE id=?",(job,))
        self.svc.worker_lock.acquire()
        try:
            with self.assertRaises(security.Problem) as ctx:self.svc.scheduler({'dry_run':False,'allow_external_effects':True})
            self.assertEqual(ctx.exception.status,409)
        finally:self.svc.worker_lock.release()
        result=self.svc.scheduler({});self.assertEqual(result['skipped_unknown'],1)

    def test_cleanup_preserves_activity_and_unverified(self):
        self.add_history();self.db.run('UPDATE messages SET cached_at=0')
        job=self.svc.enqueue('send',self.c,self.text());self.db.run("UPDATE jobs SET state='unknown' WHERE id=?",(job,))
        self.assertEqual(self.svc.cleanup(100,False),0)
        self.db.run("UPDATE jobs SET state='cancelled',result=? WHERE id=?",(self.db.seal({}),job))
        self.assertEqual(self.svc.cleanup(100,False),1)
        self.assertFalse(self.svc.conv(self.c['id'])['sync_complete'])

    def test_outbound_evidence_requires_all_three_layers(self):
        result=self.svc.manual_send(self.c['id'],[self.text()]);job=result['job_ids'][0]
        with self.assertRaises(security.Problem):self.svc.verify_outbound(job,{})
        with patch('lsp.providers.send_message',return_value={'id':'out-id'}):self.svc.drain()
        with self.assertRaises(security.Problem):self.svc.verify_outbound(job,{'omni_visible':True,'customer_received':False,'evidence':'test'})
        self.svc.verify_outbound(job,{'omni_visible':True,'customer_received':True,'evidence':'synthetic local test only'})
        self.assertTrue(self.svc.settings.passed('Webchat','manual_text'))
        self.assertEqual(self.svc.public_job(job)['state'],'accepted')

    def test_manual_unknown_retry_requires_risk_ack(self):
        result=self.svc.manual_send(self.c['id'],[self.text()]);job=result['job_ids'][0]
        self.db.run("UPDATE jobs SET state='unknown' WHERE id=?",(job,))
        with self.assertRaises(security.Problem):self.svc.resolve_job(job,{'action':'retry','evidence':'checked'})
        self.svc.resolve_job(job,{'action':'retry','evidence':'checked','ack_duplicate_risk':True})
        self.assertEqual(self.svc.public_job(job)['state'],'pending')

    def test_video_signed_url_auth_scope_and_expiry(self):
        meta={'asset_id':'guide','name':'视频','purpose':'停车指引','channels':['Webchat']}
        a=self.svc.save_asset(meta,b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42','guide.mp4','video/mp4',remote_url='https://media.example/mutable.mp4')
        self.assertEqual(self.svc.asset(a['id'])['ref'],{'local_video':True})
        s,_=self.svc.settings.get();url=self.svc.media_url(a['id'],s);token=url.rsplit('/',1)[-1]
        self.assertEqual(self.svc.media_token(token)['id'],a['id'])
        response=self.app.test_client().get('/media/'+token)
        self.assertEqual(response.status_code,200)
        response.close()
        expired=self.db.cipher.encrypt_at_time(dump({'purpose':'platform_video','asset':a['id'],'tenant':tenant_id(s)}).encode(),int(time.time())-90000).decode()
        self.assertEqual(self.app.test_client().get('/media/'+expired).status_code,403)
        self.svc.edit_asset(a['id'],{'enabled':False})
        self.assertEqual(self.app.test_client().get('/media/'+token).status_code,403)

    def test_daily_budget_and_local_rate_limit(self):
        s,_=self.svc.settings.get();payload,ctx=providers.response_payload(s,[],[])
        s['daily_token_budget']=1000
        with self.assertRaises(security.Problem) as e:self.svc.model_call(s,payload,ctx)
        self.assertEqual(e.exception.code,'daily_budget')
        s['daily_token_budget']=100000;s['ai_requests_per_minute']=1
        self.db.run('INSERT INTO usage(tenant,created,reserved) VALUES(?,?,?)',(tenant_id(s),time.time(),50))
        with self.assertRaises(security.Problem) as e:self.svc.model_call(s,payload,ctx)
        self.assertEqual(e.exception.code,'local_rate_limit')

    def test_media_auto_requires_channel_capability(self):
        a=self.svc.save_asset({'asset_id':'map','name':'入口图','purpose':'停车场入口','channels':['Webchat']},b'\x89PNG\r\n\x1a\n' + b'x'*30,'map.png','image/png')
        with patch('lsp.providers.upload',return_value=({'url':'https://media.example/map.png'},'sendable')):self.svc.upload_asset(a['id'])
        plan={'messages':[{'type':'image','text':None,'asset_id':a['id']}],'needs_human':False,'ticket_reason':None}
        self.svc.validate_plan(plan,self.c,False)
        with self.assertRaises(security.Problem):self.svc.validate_plan(plan,self.c,True)
        self.svc.settings.record('Webchat','image','passed','synthetic',{})
        self.svc.validate_plan(plan,self.c,True)

    def test_sync_cursor_ignores_newer_webhook_and_fills_gap(self):
        self.add_history(self.message('early',created='2024-01-01T00:00:01Z'))
        self.db.run("UPDATE conversations SET sync_complete=1,sync_cursor='2024-01-01T00:00:01.000Z' WHERE id=?",(self.c['id'],))
        self.add_history(self.message('newest',created='2024-01-01T00:00:03Z'))
        with patch('lsp.providers.conversation',return_value={}),patch('lsp.providers.history_pages',return_value=iter([[self.message('gap',created='2024-01-01T00:00:02Z')]])) as pages:
            self.svc.sync(self.c['id'])
        self.assertEqual(pages.call_args.args[2],'2024-01-01T00:00:01.000Z')
        self.assertEqual(self.db.one('SELECT count(*) n FROM messages')['n'],3)
        self.assertEqual(self.svc.conv(self.c['id'])['sync_cursor'],'2024-01-01T00:00:02.000Z')

    def test_new_conversation_uses_global_enable_boundary(self):
        self.enable()
        self.db.run("UPDATE meta SET value='2024-01-01T00:00:00.000Z' WHERE key='auto_since'")
        response=self.webhook(self.event(self.message('new-conv-message',conversation_id='conv-new',created='2024-01-02T00:00:00Z')))
        self.assertEqual(response.status_code,200)
        c=self.svc.add_conversation('conv-new')
        self.assertEqual(c['auto_since'],'2024-01-01T00:00:00.000Z')
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE conversation=? AND kind='generate'",(c['id'],))['n'],1)

    def test_older_event_does_not_cancel_or_regenerate_newest(self):
        self.enable();self.webhook(self.event(self.message('latest',created='2030-01-01T00:00:03Z')))
        job=self.db.one("SELECT id FROM jobs WHERE kind='generate'")['id']
        self.webhook(self.event(self.message('older',created='2030-01-01T00:00:02Z')))
        self.assertEqual(self.svc.public_job(job)['state'],'queued')
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE kind='generate'")['n'],1)

    def test_agent_origin_does_not_overwrite_customer_channel(self):
        self.enable()
        response=self.webhook(self.event(self.message('api-echo','agent',actor_id='agent-self',message_source='api')))
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.svc.conv(self.c['id'])['channel'],'Webchat')

    def test_ticket_rules_no_per_message_creation(self):
        self.svc.settings.save({'ticket_policy':'automatic','ticket_allowed_reasons':['人工跟进']})
        with self.assertRaises(security.Problem):self.svc.create_ticket(self.c['id'],'未授权原因',automatic=True)
        a=self.svc.create_ticket(self.c['id'],'人工跟进',automatic=True)
        b=self.svc.create_ticket(self.c['id'],'人工跟进',automatic=True)
        self.assertEqual(a['job_id'],b['job_id'])

    def test_file_size_limit_and_disabled_version(self):
        self.svc.settings.save({'media_size_limits':{'image':20,'video':20,'file':20}})
        with self.assertRaises(security.Problem):self.svc.save_asset({'asset_id':'map','name':'图','purpose':'入口','channels':['Webchat']},b'\x89PNG\r\n\x1a\n'+b'x'*30,'map.png','image/png')

    def test_missing_message_id_is_unknown_not_accepted(self):
        job=self.svc.manual_send(self.c['id'],[self.text()])['job_ids'][0]
        with patch('lsp.providers.send_message',return_value={}):self.svc.drain()
        self.assertEqual(self.svc.public_job(job)['state'],'unknown')

    def test_malformed_signed_payload_and_cross_conversation_history(self):
        self.assertEqual(self.webhook({'action':'message_create','data':[]}).status_code,400)
        self.assertEqual(self.webhook(self.event(self.message(message_parts=None))).status_code,400)
        with patch('lsp.providers.conversation',return_value={}),patch('lsp.providers.history_pages',return_value=iter([[self.message(conversation_id='other-conversation')]])):
            with self.assertRaises(security.Problem):self.svc.sync(self.c['id'])
        self.assertFalse(self.svc.conv(self.c['id'])['sync_complete'])
        self.assertEqual(self.db.one('SELECT count(*) n FROM messages')['n'],0)

    def test_accepted_verified_cache_can_expire(self):
        self.add_history();self.db.run('UPDATE messages SET cached_at=0')
        job=self.svc.manual_send(self.c['id'],[self.text()])['job_ids'][0]
        with patch('lsp.providers.send_message',return_value={'id':'out-confirmed'}):self.svc.drain()
        self.assertEqual(self.svc.cleanup(100,True),0)
        self.svc.verify_outbound(job,{'omni_visible':True,'customer_received':True,'evidence':'synthetic local only'})
        self.assertEqual(self.svc.cleanup(100,False),1)

    def test_wrong_channel_cannot_send_and_not_support_false_switch(self):
        self.assertEqual(self.call('/api/settings','PUT',{'integration_profile':'omni_ticket'}).status_code,400)
        self.db.run("UPDATE conversations SET channel='WhatsApp' WHERE id=?",(self.c['id'],))
        with self.assertRaises(security.Problem):self.svc.manual_send(self.c['id'],[self.text()])
        self.assertEqual(self.call('/api/checks',data={'kind':'send','confirm_send':True,'conversation_id':{},'messages':[self.text()]}).status_code,400)


if __name__=='__main__':
    unittest.main(verbosity=2)
