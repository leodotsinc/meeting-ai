#!/usr/bin/env python3
"""Qualify an image in disposable local Docker resources; never accesses a VPS.

Only newly created, random-named containers/network are removed. No prune, host
production ports, existing database, actual accounts or external AI providers.
"""
import http.cookiejar
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POSTGRES='postgres@sha256:44c4ee9810eff91f7eab4d822642e01115b1a9eccce4bcbdde7604752d68eac6'


def run(*args, input=None, timeout=120):
    result=subprocess.run(list(args),input=input,capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError('isolated qualification command failed: '+args[0])
    return result.stdout.strip()


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
    base=''
    jar=http.cookiejar.CookieJar()
    client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    def request(path,data=None,authenticated=False):
        opener=client if authenticated else urllib.request.build_opener()
        req=urllib.request.Request(base+path,data=data,headers={'Content-Type':'application/x-www-form-urlencoded','X-Auth-Return-Redirect':'1'})
        try:
            with opener.open(req,timeout=8) as response:
                return response.status,response.read(),{key.lower():value for key,value in response.headers.items()}
        except urllib.error.HTTPError as error:
            return error.code,error.read(),{key.lower():value for key,value in error.headers.items()}
    def sql(statement):
        return run('docker','exec','-i',db,'psql','-v','ON_ERROR_STOP=1','-U','ci_admin','-d','meeting_image_ci','-At',input=statement)
    try:
        run('docker','network','create',network)
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
            '-p','127.0.0.1::3003','--tmpfs','/run/cloudbox/deploy:rw','--tmpfs','/app/uploads:rw',
            '-e','DATABASE_URL=postgresql://image_runtime:'+runtime_password+'@database:5432/meeting_image_ci',
            '-e','AUTH_SECRET='+secrets.token_hex(32),'-e','AUTH_TRUST_HOST=true',
            '-e','AUTH_EMAIL=release-fixture@example.invalid','-e','AUTH_PASSWORD='+login_password,
            '-e','ENCRYPTION_KEY='+secrets.token_hex(32),'-e','UPLOAD_DIR=/app/uploads',image)
        run('docker','exec','-u','0',app,'sh','-c',
            'chmod 755 /run/cloudbox/deploy; mkdir /run/cloudbox/deploy/leases; chown 1001:1001 /run/cloudbox/deploy/leases /app/uploads; chmod 700 /run/cloudbox/deploy/leases')
        port=run('docker','port',app,'3003/tcp').rsplit(':',1)[1]
        base='http://127.0.0.1:'+port
        for _ in range(90):
            try:
                status,body,_=request('/api/health')
                if status==200 and json.loads(body)=={'status':'ok'}:break
            except (OSError,ValueError):pass
            time.sleep(1)
        else:raise RuntimeError('isolated app readiness failed')
        assert request('/api/version')[0]==401
        assert request('/api/stats')[0]==401
        status,body,_=request('/api/auth/csrf',authenticated=True)
        assert status==200
        csrf=json.loads(body)['csrfToken']
        data=urllib.parse.urlencode({'csrfToken':csrf,'email':'release-fixture@example.invalid',
                                   'password':login_password,'callbackUrl':base+'/','json':'true'}).encode()
        assert request('/api/auth/callback/credentials',data,True)[0]==200
        status,body,headers=request('/api/version',authenticated=True)
        metadata=json.loads(body)
        info=json.loads(run('docker','image','inspect',image))[0]
        env=dict(item.split('=',1) for item in info['Config']['Env'] if '=' in item)
        assert status==200 and metadata=={'version':env['APP_VERSION'],'revision':env['APP_REVISION'],'build_id':env['APP_BUILD_ID']}
        assert 'no-store' in headers.get('cache-control','')
        assert request('/api/stats',authenticated=True)[0]==200
        assert sql('SELECT count(*) FROM public."User";')=='1'
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
            'external_processing':False,'production_restore':False}))
    finally:
        for kind,name in reversed(made):
            subprocess.run(['docker',kind,'rm',*(['-f'] if kind=='container' else []),name],capture_output=True,timeout=30)


if __name__=='__main__':
    qualify(sys.argv[1])
