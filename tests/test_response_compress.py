import gzip
import json
import unittest

from flask import Flask, Response, jsonify

import response_compress


def make_app():
    app = Flask(__name__)
    response_compress.install(app)

    @app.route('/big')
    def big():
        return jsonify({'rows': [{'display': '神秘人602191', 'tickets': 7694}] * 200})

    @app.route('/small')
    def small():
        return jsonify({'ok': True})

    @app.route('/stream')
    def stream():
        def gen():
            yield 'data: hello\n\n'
        return Response(gen(), mimetype='text/event-stream')

    @app.route('/image')
    def image():
        return Response(b'\x89PNG' + b'x' * 5000, mimetype='image/png')

    @app.route('/already')
    def already():
        resp = Response(b'x' * 5000, mimetype='application/json')
        resp.headers['Content-Encoding'] = 'br'
        return resp

    return app


class CompressTests(unittest.TestCase):
    def setUp(self):
        self.client = make_app().test_client()

    def test_large_json_is_gzipped_and_decodes_back(self):
        raw = self.client.get('/big').get_data()
        resp = self.client.get('/big', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual('gzip', resp.headers.get('Content-Encoding'))
        self.assertIn('Accept-Encoding', resp.headers.get('Vary', ''))
        restored = gzip.decompress(resp.get_data())
        self.assertEqual(json.loads(raw), json.loads(restored))
        self.assertLess(len(resp.get_data()), len(raw) / 2, '至少要压掉一半')

    def test_streaming_response_is_never_touched(self):
        """SSE 是无限流：压缩钩子若去读它的内容会把流缓冲住、直接挂死公屏。"""
        resp = self.client.get('/stream', headers={'Accept-Encoding': 'gzip'})
        self.assertIsNone(resp.headers.get('Content-Encoding'))
        self.assertIn('text/event-stream', resp.content_type)

    def test_client_without_gzip_support_gets_plain(self):
        resp = self.client.get('/big', headers={'Accept-Encoding': 'identity'})
        self.assertIsNone(resp.headers.get('Content-Encoding'))

    def test_small_response_is_not_compressed(self):
        resp = self.client.get('/small', headers={'Accept-Encoding': 'gzip'})
        self.assertIsNone(resp.headers.get('Content-Encoding'))

    def test_binary_type_is_not_compressed(self):
        resp = self.client.get('/image', headers={'Accept-Encoding': 'gzip'})
        self.assertIsNone(resp.headers.get('Content-Encoding'))

    def test_already_encoded_response_is_left_alone(self):
        resp = self.client.get('/already', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual('br', resp.headers.get('Content-Encoding'))

    def test_content_length_matches_compressed_body(self):
        resp = self.client.get('/big', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(len(resp.get_data()),
                         int(resp.headers['Content-Length']))


if __name__ == '__main__':
    unittest.main()
