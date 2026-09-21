"""No Docker/HTTP access: execute the real in-container client against fake fetch."""
import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('qualify_image',ROOT/'scripts/qualify-image.py')
q=importlib.util.module_from_spec(spec);spec.loader.exec_module(q)


class InternalHttpTests(unittest.TestCase):
    def execute_client(self,*args,input=None,timeout=None):
        self.assertEqual(args[:5],('docker','exec','-i','synthetic-app','node'))
        self.assertEqual(timeout,12)
        fake="""globalThis.fetch=async(url,options)=>{
          if(url.origin!=='http://127.0.0.1:3003'||options.redirect!=='manual')throw Error('unsafe request');
          const body=JSON.stringify({path:url.pathname,method:options.method,cookie:options.headers.Cookie,data:options.body});
          return {status:200,body:(async function*(){yield Buffer.from(body)})(),
            headers:{getSetCookie:()=>['authjs.session-token=synthetic; Path=/; HttpOnly'],
              *[Symbol.iterator](){yield ['cache-control','no-store']}}};
        };"""
        result=subprocess.run(['node','-e',fake+args[-1]],input=input,capture_output=True,text=True,timeout=5)
        if result.returncode:raise RuntimeError('client refused unsafe input')
        return result.stdout

    def test_authenticated_post_retains_session_without_host_http(self):
        cookies={'authjs.csrf-token':'fixture'}
        with patch.object(q,'run',side_effect=self.execute_client):
            status,body,headers=q.local_request('synthetic-app','/api/auth/callback/credentials',b'a=b',cookies)
        value=json.loads(body)
        self.assertEqual(status,200);self.assertEqual(value['method'],'POST')
        self.assertEqual(value['data'],'a=b');self.assertEqual(value['cookie'],'authjs.csrf-token=fixture')
        self.assertEqual(cookies['authjs.session-token'],'synthetic')
        self.assertEqual(headers['cache-control'],'no-store')

    def test_anonymous_get_sends_no_cookie(self):
        with patch.object(q,'run',side_effect=self.execute_client):
            _,body,_=q.local_request('synthetic-app','/api/health')
        self.assertEqual(json.loads(body)['cookie'],'')
        self.assertEqual(json.loads(body)['method'],'GET')

    def test_absolute_network_path_and_header_injection_never_execute(self):
        for path in ('https://example.invalid','//example.invalid','/api/health\r\nHost: x'):
            with self.subTest(path=path),patch.object(q,'run') as run:
                with self.assertRaises(ValueError):q.local_request('synthetic-app',path)
                run.assert_not_called()

    def test_backslash_origin_escape_refused_by_actual_url_parser(self):
        with patch.object(q,'run',side_effect=self.execute_client):
            with self.assertRaises(RuntimeError):q.local_request('synthetic-app','/\\example.invalid')

    def test_oversize_response_aborts_instead_of_returning_partial_proof(self):
        def oversized(*args,input=None,timeout=None):
            fake="globalThis.fetch=async()=>({body:(async function*(){yield Buffer.alloc(1048577)})()});"
            result=subprocess.run(['node','-e',fake+args[-1]],input=input,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(result.returncode,0)
            raise RuntimeError('bounded client refused')
        with patch.object(q,'run',side_effect=oversized):
            with self.assertRaises(RuntimeError):q.local_request('synthetic-app','/api/health')


if __name__=='__main__':unittest.main()
