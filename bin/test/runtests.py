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

import redis
from tornado import gen
from tornado.concurrent import Future
from tornado.escape import json_decode, json_encode
from tornado.options import options
from tornado.test.util import unittest
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import Application
from tornado.websocket import websocket_connect

from bin.blackmagic import RPCHandler

# jwt.encode({'user_id': 1, 'ip': '127.0.0.1'}, 'secret', algorithm='HS256')
ENCODED_TOKEN = 'eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.' \
                'eyJpcCI6IjEyNy4wLjAuMSIsInVzZXJfaWQiOjF9.' \
                'kYIAQYDjOiZpjExvXZaAgemi4xiisvPEzvXEemmAJLY'
TOKEN_KEY = 'secret'


class MockRPCServer(RPCHandler):
    def initialize(self, close_future, compression_options=None):
        self.close_future = close_future
        self.compression_options = compression_options

    def get_compression_options(self):
        return self.compression_options

    def on_close(self):
        self.close_future.set_result((self.close_code, self.close_reason))


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
        redis_conn.set(ENCODED_TOKEN, '')
        options.token_key = TOKEN_KEY
        return Application([
            ('/rpc/token/([\w\.]+)', MockRPCServer,
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
    def test_getting_packages_number(self):
        ws = yield self.ws_connect('/rpc/token/{}'.format(ENCODED_TOKEN))
        payload = self.prepare_payload('get_packages_number', [], 1)
        ws.write_message(payload)
        response = yield ws.read_message()
        self.assertEqual(json_decode(response), {
            'result': 0,
            'marker': 1
        })
        yield self.close(ws)

if __name__ == '__main__':
    unittest.main()
