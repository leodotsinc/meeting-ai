#!/usr/bin/env python3
"""Qualify an image in disposable local Docker resources; never accesses a VPS.

Only newly created, random-named containers/network are removed. No prune, host
production ports, existing database, actual accounts or external AI providers.
"""
import base64
import hashlib
from http.cookies import SimpleCookie
import json
import secrets
import subprocess
import sys
import time
import urllib.parse

POSTGRES='postgres@sha256:44c4ee9810eff91f7eab4d822642e01115b1a9eccce4bcbdde7604752d68eac6'


def run(*args, input=None, timeout=120):
    result=subprocess.run(list(args),input=input,capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError('isolated qualification command failed: '+args[0])
    return result.stdout.strip()


def local_request(app, path, data=None, cookies=None):
    """Bounded loopback HTTP inside the no-egress fixture; never a host URL."""
    if not path.startswith('/') or path.startswith('//') or any(c in path for c in '\r\n'):
        raise ValueError('INVALID_FIXTURE_PATH')
    payload = {'path': path, 'data': None if data is None else data.decode(),
               'cookies': cookies or {}}
    script = r"""
      let raw='';process.stdin.on('data',c=>raw+=c);process.stdin.on('end',async()=>{
        try {
          const x=JSON.parse(raw),url=new URL(x.path,'http://127.0.0.1:3003');
          if(url.origin!=='http://127.0.0.1:3003')throw Error('origin');
          const r=await fetch(url,{method:x.data===null?'GET':'POST',redirect:'manual',
            signal:AbortSignal.timeout(8000),headers:{'Content-Type':'application/x-www-form-urlencoded',
              'X-Auth-Return-Redirect':'1','Cookie':Object.entries(x.cookies).map(([k,v])=>k+'='+v).join('; ')},body:x.data});
          let size=0,chunks=[];for await(const chunk of r.body){size+=chunk.length;if(size>1048576)throw Error('body');chunks.push(chunk)}
          console.log(JSON.stringify({status:r.status,body:Buffer.concat(chunks).toString('base64'),
            headers:Object.fromEntries(r.headers),cookies:r.headers.getSetCookie()}));
        }catch{process.exit(1)}
      });
    """
    value=json.loads(run('docker','exec','-i',app,'node','-e',script,
                         input=json.dumps(payload),timeout=12))
    if cookies is not None:
        for header in value['cookies']:
            parsed=SimpleCookie();parsed.load(header)
            for key,item in parsed.items(): cookies[key]=item.value
    return value['status'],base64.b64decode(value['body'],validate=True),value['headers']


def qualify(image):
    token=secrets.token_hex(5)
    network='meeting-image-ci-'+token
    db=network+'-db'
    app=network+'-app'
    migrator=network+'-migrator'
    migration_check=network+'-migration-check'
    migration_database='meeting_migrations_ci_'+token
    made=[]
    password=secrets.token_hex(24)
    runtime_password=secrets.token_hex(24)
    login_password=secrets.token_hex(24)
    cookies={}
    def request(path,data=None,authenticated=False):
        return local_request(app,path,data,cookies if authenticated else None)
    def sql(statement):
        return run('docker','exec','-i',db,'psql','-v','ON_ERROR_STOP=1','-U','ci_admin','-d','meeting_image_ci','-At',input=statement)
    try:
        run('docker','network','create','--internal',network)
        made.append(('network',network))
        made.append(('container',db))
        run('docker','run','-d','--name',db,'--network',network,'--network-alias','database',
            '--memory','512m','--cpus','1','--tmpfs','/var/lib/postgresql/data:rw',
            '-e','POSTGRES_USER=ci_admin','-e','POSTGRES_PASSWORD='+password,
            '-e','POSTGRES_DB=meeting_image_ci',POSTGRES)
        for _ in range(60):
            try:
                sql('SELECT 1;');break
            except RuntimeError:time.sleep(1)
        else:raise RuntimeError('isolated database not ready')
        # Exercise resolve/diff/deploy using the same read-only filesystem and
        # temporary writable area required by the protected host helper. The
        # loopback is this disposable database container's namespace, never VPS.
        sql('CREATE DATABASE '+migration_database+';')
        made.append(('container',migration_check))
        migration_proof=json.loads(run('docker','run','--rm','--name',migration_check,
            '--network','container:'+db,'--read-only','--tmpfs','/tmp:rw,nosuid,size=64m',
            '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','128',
            '--memory','512m','--cpus','1','-e','HOME=/tmp','-e','CHECKPOINT_DISABLE=1',
            '-e','DATABASE_URL=postgresql://ci_admin:'+password+'@127.0.0.1:5432/'+migration_database,
            '--entrypoint','node',image,'scripts/qualify-migrations.mjs',timeout=480))
        assert migration_proof['ok'] is True and len(migration_proof['checks'])==15
        made.append(('container',migrator))
        run('docker','run','--rm','--name',migrator,'--network',network,'-e',
            'DATABASE_URL=postgresql://ci_admin:'+password+'@database:5432/meeting_image_ci',
            '--entrypoint','node',image,'node_modules/prisma/build/index.js','migrate','deploy',timeout=360)
        sql('CREATE ROLE image_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '+"'"+runtime_password+"';"+
            'GRANT CONNECT ON DATABASE meeting_image_ci TO image_runtime; GRANT USAGE ON SCHEMA public TO image_runtime; '+
            'GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO image_runtime; '+
            'REVOKE ALL ON public._prisma_migrations FROM image_runtime;')
        made.append(('container',app))
        run('docker','run','-d','--name',app,'--network',network,'--memory','768m','--cpus','1',
            '--tmpfs','/run/cloudbox/deploy:rw','--tmpfs','/app/uploads:rw',
            '-e','DATABASE_URL=postgresql://image_runtime:'+runtime_password+'@database:5432/meeting_image_ci',
            '-e','AUTH_SECRET='+secrets.token_hex(32),'-e','AUTH_TRUST_HOST=true',
            '-e','AUTH_EMAIL=release-fixture@example.invalid','-e','AUTH_PASSWORD='+login_password,
            '-e','ENCRYPTION_KEY='+secrets.token_hex(32),'-e','UPLOAD_DIR=/app/uploads',image)
        run('docker','exec','-u','0',app,'sh','-c',
            'chmod 755 /run/cloudbox/deploy; mkdir /run/cloudbox/deploy/leases; chown 1001:1001 /run/cloudbox/deploy/leases /app/uploads; chmod 700 /run/cloudbox/deploy/leases')
        for _ in range(90):
            try:
                status,body,_=request('/api/health')
                if status==200 and json.loads(body)=={'status':'ok'}:break
            except (OSError,ValueError,RuntimeError):pass
            time.sleep(1)
        else:raise RuntimeError('isolated app readiness failed')
        assert request('/api/version')[0]==401
        assert request('/api/stats')[0]==401
        status,body,_=request('/api/auth/csrf',authenticated=True)
        assert status==200
        csrf=json.loads(body)['csrfToken']
        data=urllib.parse.urlencode({'csrfToken':csrf,'email':'release-fixture@example.invalid',
                                   'password':login_password,'callbackUrl':'http://127.0.0.1:3003/','json':'true'}).encode()
        assert request('/api/auth/callback/credentials',data,True)[0]==200
        status,body,headers=request('/api/version',authenticated=True)
        metadata=json.loads(body)
        info=json.loads(run('docker','image','inspect',image))[0]
        env=dict(item.split('=',1) for item in info['Config']['Env'] if '=' in item)
        assert status==200 and metadata=={'version':env['APP_VERSION'],'revision':env['APP_REVISION'],'build_id':env['APP_BUILD_ID']}
        assert 'no-store' in headers.get('cache-control','')
        assert request('/api/stats',authenticated=True)[0]==200
        assert sql('SELECT count(*) FROM public."User";')=='1'
        # Populate only synthetic local data. No processing/upload endpoint is
        # invoked: those may contact providers and do not belong in this fixture.
        sql('INSERT INTO public."Project" (id,"userId",name,color,"createdAt","updatedAt") '
            "SELECT 'maintenance-fixture',id,'Maintenance fixture','#71717a',now(),now() FROM public.\"User\";")
        status,body,_=request('/api/projects',authenticated=True)
        projects=json.loads(body)
        assert status==200 and len(projects)==1 and projects[0]['id']=='maintenance-fixture'
        original=b'Synthetic maintenance upload; no personal data.\n'
        original_hash=hashlib.sha256(original).hexdigest()
        run('docker','exec','-i',app,'node','-e',
            "let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>require('fs').writeFileSync('/app/uploads/maintenance-fixture.txt',Buffer.from(s,'base64')))",
            input=base64.b64encode(original).decode())
        upload_backup=run('docker','exec',app,'node','-e',
            "console.log(require('fs').readFileSync('/app/uploads/maintenance-fixture.txt').toString('base64'))")
        assert base64.b64decode(upload_backup,validate=True)==original
        # A logical backup is restored into a NEW database; never over current
        # data. Dump output and credentials remain in memory and are not logged.
        dump=run('docker','exec',db,'pg_dump','-U','ci_admin','-d','meeting_image_ci','--no-owner','--no-acl',timeout=60)
        assert len(dump.encode())<4*1024*1024 and 'maintenance-fixture' in dump
        restored='meeting_restore_ci_'+token
        sql('CREATE DATABASE '+restored+';')
        run('docker','exec','-i',db,'psql','-v','ON_ERROR_STOP=1','-U','ci_admin','-d',restored,input=dump,timeout=60)
        restored_rows=run('docker','exec',db,'psql','-U','ci_admin','-d',restored,'-At','-c',
            'SELECT id FROM public."Project";')
        assert restored_rows=='maintenance-fixture'
        run('docker','exec','-i',db,'psql','-v','ON_ERROR_STOP=1','-U','ci_admin','-d',restored,
            input='GRANT USAGE ON SCHEMA public TO image_runtime; GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO image_runtime; REVOKE ALL ON public._prisma_migrations FROM image_runtime;')
        # A second instance serves only the separately restored database/media.
        # Reuse synthetic auth secrets so the original session must still work.
        restored_app=network+'-restored'
        info=json.loads(run('docker','image','inspect',image))[0]
        app_info=json.loads(run('docker','container','inspect',app))[0]
        env=dict(item.split('=',1) for item in app_info['Config']['Env'] if '=' in item)
        made.append(('container',restored_app))
        arguments=['docker','run','-d','--name',restored_app,'--network',network,'--memory','768m','--cpus','1',
                   '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','128','--tmpfs','/app/uploads:rw,uid=1001,gid=1001,mode=0700']
        for key in ('AUTH_SECRET','AUTH_TRUST_HOST','AUTH_EMAIL','AUTH_PASSWORD','ENCRYPTION_KEY','UPLOAD_DIR'):
            arguments.extend(['-e',key+'='+env[key]])
        arguments.extend(['-e','DATABASE_URL=postgresql://image_runtime:'+runtime_password+'@database:5432/'+restored,image])
        run(*arguments)
        run('docker','exec','-i',restored_app,'node','-e',
            "let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>require('fs').writeFileSync('/app/uploads/maintenance-fixture.txt',Buffer.from(s,'base64')))",
            input=upload_backup)
        for _ in range(60):
            try:
                status,body,_=local_request(restored_app,'/api/projects',cookies=dict(cookies))
                if status==200:break
            except (OSError,ValueError,RuntimeError):pass
            time.sleep(1)
        else:raise RuntimeError('restored application did not preserve authentication')
        assert json.loads(body)==projects
        restored_hash=run('docker','exec',restored_app,'node','-e',
            "console.log(require('crypto').createHash('sha256').update(require('fs').readFileSync('/app/uploads/maintenance-fixture.txt')).digest('hex'))")
        assert restored_hash==original_hash
        # The original application/database remained available and unchanged.
        assert json.loads(request('/api/projects',authenticated=True)[1])==projects
        # Drain blocks real authenticated mutation and login, leaves reads healthy.
        run('docker','exec','-u','0',app,'touch','/run/cloudbox/deploy/draining')
        assert request('/api/auth/callback/credentials',data,True)[0]==503
        assert request('/api/health')[0]==200
        assert request('/api/version',authenticated=True)[0]==200
        run('docker','exec','-u','0',app,'rm','/run/cloudbox/deploy/draining')
        assert run('docker','exec',app,'sh','-c','ls -A /run/cloudbox/deploy/leases')==''
        print(json.dumps({'ok':True,'image_id':info['Id'],'version':metadata['version'],
            'migration_checks':migration_proof['checks'],
            'migration_readonly_image':True,'checks':['readiness_database','anonymous_auth_boundary','fixture_login',
                      'authenticated_version','runtime_database_stats','drain_blocks_mutations','leases_released'],
            'external_processing':False,'production_restore':False,
            'network_internal':True,'ports_published':False,'model_calls':0,
            'synthetic_restore':{'database_separate':True,'authenticated_projects':True,
                'existing_session_preserved':True,'original_application_preserved':True,
                'upload_sha256':original_hash,'restored_upload_sha256':restored_hash},
            'candidate_upgrade_and_downgrade_qualified':False}))
    finally:
        for kind,name in reversed(made):
            subprocess.run(['docker',kind,'rm',*(['-f'] if kind=='container' else []),name],capture_output=True,timeout=30)


if __name__=='__main__':
    qualify(sys.argv[1])
