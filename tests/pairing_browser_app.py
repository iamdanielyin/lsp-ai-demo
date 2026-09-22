"""Loopback-only synthetic browser fixture. Never run against a real tenant."""
import base64
import itertools
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from flask import request
from waitress import serve
from lsp.app import create_app


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='lsp_pairing_ui_') as directory:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        app = create_app({'DATABASE_PATH': directory+'/test.sqlite3', 'FILE_DIRECTORY': directory+'/files',
                          'CONFIG_MASTER_KEY': Fernet.generate_key().decode(),
                          'ADMIN_INITIAL_PASSWORD': 'LOCAL-BROWSER-TEST-ONLY'}, start_worker=False)
        svc = app.extensions['service']
        svc.settings.save({'platform_api_base_url': 'https://browser-test.freshchat.com', 'freshchat_token': 'LOCAL_TEST_TOKEN',
                           'reply_actor_id': 'LOCAL_TEST_AGENT', 'openai_api_key': 'LOCAL_TEST_KEY', 'openai_model': 'LOCAL_TEST_MODEL',
                           'public_base_url': 'https://local-test.example',
                           'freshchat_public_key': key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()})
        rows, counter = {}, itertools.count(1)

        def signed_event(message):
            raw = json.dumps({'action': 'message_create', 'data': {'message': message}}).encode()
            signature = base64.b64encode(key.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()
            with app.test_client() as client:
                response = client.post('/api/webhooks/freshchat', data=raw, headers={
                    'Content-Type': 'application/json', 'X-Freshchat-Signature': signature})
                return response.json, response.status_code

        # Fixture-only endpoint. Normal admin/CSRF checks still apply; production never mounts it.
        @app.post('/api/local-test-message')
        def incoming():
            data = request.get_json()
            channel = data['channel']
            assert channel in ('WhatsApp', 'WeChat')
            cid = 'LOCAL_TEST_' + channel
            message = {'id': 'LOCAL_TEST_IN_'+str(next(counter)), 'conversation_id': cid,
                       'user_id': 'LOCAL_TEST_USER_'+channel, 'actor_id': 'LOCAL_TEST_USER_'+channel,
                       'actor_type': 'user', 'message_type': 'normal', 'message_source': channel.lower(),
                       'created_time': datetime.now(timezone.utc).isoformat(),
                       'message_parts': [{'text': {'content': data['text']}}]}
            rows.setdefault(cid, []).append(message)
            return signed_event(message)

        def send(settings, cid, message, asset=None):
            row = {'id': 'LOCAL_TEST_OUT_'+str(next(counter)), 'conversation_id': cid,
                   'user_id': rows[cid][0]['user_id'], 'actor_id': 'LOCAL_TEST_AGENT', 'actor_type': 'agent',
                   'message_type': 'normal', 'created_time': datetime.now(timezone.utc).isoformat(),
                   'message_parts': [{'text': {'content': message.get('text') or '[合成媒体]'}}]}
            rows[cid].append(row)
            signed_event(row)
            return {'id': row['id']}

        def model(*args):
            plan = {'messages': [{'type': 'text', 'text': '本地合成模型回复，仅用于验证界面和流程。', 'asset_id': None}],
                    'needs_human': False, 'ticket_reason': None}
            return {'status': 'completed', 'id': 'LOCAL_TEST_RESPONSE',
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(plan)}]}],
                    'usage': {'total_tokens': 150}}, 'LOCAL_TEST_REQUEST'

        with patch('lsp.providers.conversation', side_effect=lambda s, cid: {'conversation_id': cid}), \
             patch('lsp.providers.history_pages', side_effect=lambda s, cid, *args: iter([rows[cid].copy()])), \
             patch('lsp.providers.openai', side_effect=model), patch('lsp.providers.send_message', side_effect=send):
            svc.start()
            print('LOCAL SYNTHETIC PAIRING TEST ONLY http://127.0.0.1:8128', flush=True)
            serve(app, host='127.0.0.1', port=8128, threads=4)
