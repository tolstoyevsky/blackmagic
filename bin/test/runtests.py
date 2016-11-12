# Copyright 2016 Evgeny Golyshev. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import jwt
import redis
from shirow.server import TOKEN_PATTEN
from tornado import gen
from tornado.concurrent import Future
from tornado.escape import json_decode, json_encode
from tornado.options import options
from tornado.test.util import unittest
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import Application
from tornado.websocket import websocket_connect

from bin.blackmagic import \
    DEFAULT_ROOT_PASSWORD, NOT_INITIALIZED, READY, RPCHandler

TOKEN_ALGORITHM_ENCODING = 'HS256'

TOKEN_KEY = 'secret'

TOKEN_TTL = 15

USER_ID = 1

ENCODED_TOKEN = jwt.encode({'user_id': USER_ID, 'ip': '127.0.0.1'}, TOKEN_KEY,
                           algorithm=TOKEN_ALGORITHM_ENCODING).decode('utf8')


class LockedRPCServer(RPCHandler):
    def __init__(self, application, request, **kwargs):
        RPCHandler.__init__(self, application, request, **kwargs)

    def initialize(self, close_future, compression_options=None):
        self.close_future = close_future
        self.compression_options = compression_options

    def get_compression_options(self):
        return self.compression_options

    def on_close(self):
        self.close_future.set_result((self.close_code, self.close_reason))


class UnlockedRPCServer(LockedRPCServer):
    def __init__(self, application, request, **kwargs):
        LockedRPCServer.__init__(self, application, request, **kwargs)
        self.global_lock = False


class WebSocketBaseTestCase(AsyncHTTPTestCase):
    @gen.coroutine
    def ws_connect(self, path, compression_options=None):
        ws = yield websocket_connect('ws://127.0.0.1:{}{}'.format(
            self.get_http_port(), path
        ), compression_options=compression_options)
        raise gen.Return(ws)

    @gen.coroutine
    def close(self, ws):
        """Close a websocket connection and wait for the server side.

        If we don't wait here, there are sometimes leak warnings in the
        tests.
        """
        ws.close()
        yield self.close_future


class RPCServerTest(WebSocketBaseTestCase):
    def get_app(self):
        self.close_future = Future()
        redis_conn = redis.StrictRedis(host='localhost', port=6379, db=0)
        key = 'user:{}:token'.format(USER_ID)
        redis_conn.setex(key, 60 * TOKEN_TTL, ENCODED_TOKEN)
        options.token_algorithm = TOKEN_ALGORITHM_ENCODING
        options.token_key = TOKEN_KEY
        return Application([
            ('/locked_rpc/token/' + TOKEN_PATTEN, LockedRPCServer,
             dict(close_future=self.close_future)),
            ('/unlocked_rpc/token/' + TOKEN_PATTEN, UnlockedRPCServer,
             dict(close_future=self.close_future)),
        ])

    def prepare_payload(self, procedure_name, parameters_list, marker):
        data = {
            'function_name': procedure_name,
            'parameters_list': parameters_list,
            'marker': marker
        }
        return json_encode(data)

    @gen_test
    def test_locked_behaviour(self):
        ws = yield self.ws_connect('/locked_rpc/token/' + ENCODED_TOKEN)
        payload = self.prepare_payload('change_root_password', ['stub'], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        self.assertEqual(json_decode(response), {
            'result': NOT_INITIALIZED,
            'marker': 1,
            'eod': 1,
        })
        yield self.close(ws)

    @gen_test
    def test_changing_root_password(self):
        ws = yield self.ws_connect('/unlocked_rpc/token/' + ENCODED_TOKEN)
        payload = self.prepare_payload('change_root_password', ['stub'], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        self.assertEqual(json_decode(response), {
            'result': READY,
            'marker': 1,
            'eod': 1,
        })
        yield self.close(ws)

    @gen_test
    def test_getting_default_root_password(self):
        ws = yield self.ws_connect('/unlocked_rpc/token/' + ENCODED_TOKEN)
        payload = self.prepare_payload('get_default_root_password', [], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        self.assertEqual(json_decode(response), {
            'result': DEFAULT_ROOT_PASSWORD,
            'marker': 1,
            'eod': 1,
        })
        yield self.close(ws)

    @gen_test
    def test_getting_packages_number(self):
        ws = yield self.ws_connect('/unlocked_rpc/token/' + ENCODED_TOKEN)
        payload = self.prepare_payload('get_packages_number', [], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        decoded_response = json_decode(response)
        # The main section of the official Debian archive includes more than
        # 41000 binary packages. The exact number may vary slightly from one
        # minor release of the distribution to another.
        self.assertEqual(True, decoded_response['result'] > 41000)
        yield self.close(ws)

    @gen_test
    def test_getting_packages_list(self):
        ws = yield self.ws_connect('/unlocked_rpc/token/' + ENCODED_TOKEN)
        page_number = 0
        per_page = 5
        payload = self.prepare_payload('get_packages_list', [page_number,
                                                             per_page], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        d = json_decode(response)
        self.assertEqual(len(d['result']), per_page)
        yield self.close(ws)

    @gen_test
    def test_searching(self):
        ws = yield self.ws_connect('/unlocked_rpc/token/' + ENCODED_TOKEN)
        payload = self.prepare_payload('search', ['nginx'], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        decoded_response = json_decode(response)
        packages_names = [doc['package'] for doc in decoded_response['result']]
        expected = {
            'lua-nginx-memcached',
            'lua-nginx-redis',
            'lua-nginx-websocket',
            'nginx',
            'nginx-common',
            'nginx-doc',
            'nginx-extras',
            'nginx-extras-dbg',
            'nginx-full',
            'nginx-full-dbg',
            'nginx-light',
            'nginx-light-dbg'
        }
        self.assertEqual(expected, set(packages_names))
        yield self.close(ws)

    @gen_test
    def test_sync_image_configuration_parameters_empty_json(self):
        ws = yield self.ws_connect('/unlocked_rpc/token/' + ENCODED_TOKEN)
        payload = self.prepare_payload('sync_configuration', [{}], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        self.assertEqual(json_decode(response), {
            'result': READY,
            'marker': 1,
            'eod': 1,
        })
        yield self.close(ws)

    @gen_test
    def test_sync_image_configuration_parameters_non_empty_json(self):
        ws = yield self.ws_connect('/unlocked_rpc/token/' + ENCODED_TOKEN)
        payload = self.prepare_payload('sync_configuration', [
            {'HOSTNAME': 'cusdebtesthostname'}
        ], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        self.assertEqual(json_decode(response), {
            'result': READY,
            'marker': 1,
            'eod': 1,
        })
        yield self.close(ws)

if __name__ == '__main__':
    unittest.main()
