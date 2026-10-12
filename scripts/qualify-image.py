#!/usr/bin/env python3
"""Qualify an image in disposable local Docker resources; never accesses a VPS.

Only newly created, random-named containers/network are removed. No prune, host
production ports, existing database, actual accounts or external AI providers.
"""
import base64
import argparse
from datetime import datetime, timezone
import hashlib
from http.cookies import SimpleCookie
import json
import re
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


def local_request(app, path, data=None, cookies=None, *, method=None, content_type='application/x-www-form-urlencoded'):
    """Bounded loopback HTTP inside the no-egress fixture; never a host URL."""
    if not path.startswith('/') or path.startswith('//') or any(c in path for c in '\r\n'):
        raise ValueError('INVALID_FIXTURE_PATH')
    if method not in (None, 'GET', 'POST', 'PATCH', 'DELETE') or content_type not in ('application/x-www-form-urlencoded', 'application/json'):
        raise ValueError('INVALID_FIXTURE_REQUEST')
    payload = {'path': path, 'data': None if data is None else data.decode(),
               'method': method or ('GET' if data is None else 'POST'), 'contentType': content_type,
               'cookies': cookies or {}}
    script = r"""
      let raw='';process.stdin.on('data',c=>raw+=c);process.stdin.on('end',async()=>{
        try {
          const x=JSON.parse(raw),url=new URL(x.path,'http://127.0.0.1:3003');
          if(url.origin!=='http://127.0.0.1:3003')throw Error('origin');
          const r=await fetch(url,{method:x.method,redirect:'manual',
            signal:AbortSignal.timeout(8000),headers:{'Content-Type':x.contentType,
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


def require(condition, code):
    if not condition:
        raise ValueError(code)


def image_metadata(image, *, revision=None, version=None, build_id=None, immutable=False):
    """Resolve once; subsequent container runs use the inspected config digest."""
    require(not immutable or re.fullmatch(r'[a-z0-9./_-]+@sha256:[0-9a-f]{64}', image),
            'IMMUTABLE_BASELINE_REQUIRED')
    info=json.loads(run('docker','image','inspect',image))[0]
    env=dict(item.split('=',1) for item in info['Config']['Env'] if '=' in item)
    labels=info['Config'].get('Labels') or {}
    result={'image':image,'image_id':info['Id'],'version':env.get('APP_VERSION'),
            'revision':env.get('APP_REVISION'),'build_id':env.get('APP_BUILD_ID'),
            'build_timestamp':info.get('Created')}
    require(re.fullmatch(r'sha256:[0-9a-f]{64}',result['image_id']), 'INVALID_IMAGE_ID')
    require(re.fullmatch(r'[0-9a-f]{40}',result['revision'] or ''), 'INVALID_IMAGE_REVISION')
    require(re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',result['version'] or ''), 'INVALID_IMAGE_VERSION')
    require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}',result['build_id'] or ''), 'INVALID_IMAGE_BUILD_ID')
    require(result['build_timestamp'] and 'T' in result['build_timestamp'], 'MISSING_BUILD_TIMESTAMP')
    require(labels.get('org.opencontainers.image.version')==result['version'] and
            labels.get('org.opencontainers.image.revision')==result['revision'], 'IMAGE_LABEL_MISMATCH')
    for key,expected in (('revision',revision),('version',version),('build_id',build_id)):
        require(expected is None or result[key]==expected, 'EXPECTED_IMAGE_'+key.upper()+'_MISMATCH')
    if immutable:
        require(image in info.get('RepoDigests',[]), 'BASELINE_DIGEST_MISMATCH')
    return result


FIXTURE_TABLES=('User','Project','Meeting','Tag','MeetingTag','Transcript','Speaker','Utterance','Analysis','Setting')
CROSS_VERSION_CHECKS=('same_database','same_upload_volume','baseline_authenticated',
    'candidate_reads_baseline_data','candidate_preserves_baseline_session','candidate_writes_verified',
    'rollback_reads_candidate_data','rollback_preserves_baseline_session','rollback_preserves_candidate_session',
    'rollback_writes_verified','schema_unchanged','migration_history_unchanged','uploads_preserved','recovery_verified')


def verify_data_transition(before, after, mutable_fields, added_rows=None):
    """Accept only the intended API mutations; collateral changes fail closed."""
    added_rows=added_rows or {}
    for table in FIXTURE_TABLES:
        require(len(after[table])==len(before[table])+added_rows.get(table,0), 'UNEXPECTED_FIXTURE_ROW_COUNT')
        if table not in mutable_fields and table not in added_rows:
            require(after[table]==before[table], 'UNEXPECTED_FIXTURE_DATA_CHANGE')
            continue
        ignored=mutable_fields.get(table,set())
        for old in before[table]:
            matches=[row for row in after[table] if row['id']==old['id']]
            require(len(matches)==1 and
                    {k:v for k,v in matches[0].items() if k not in ignored}==
                    {k:v for k,v in old.items() if k not in ignored}, 'UNEXPECTED_FIXTURE_DATA_CHANGE')


class CrossVersionFixture:
    """Only random-named, newly created Docker resources and synthetic secrets."""
    def __init__(self):
        self.token=secrets.token_hex(5)
        self.network='meeting-image-ci-'+self.token
        self.db=self.network+'-db'
        self.database='meeting_image_ci'
        self.volume=self.network+'-uploads'
        self.made=[]
        self.serial=0
        self.password=secrets.token_hex(24)
        self.migrator_password=secrets.token_hex(24)
        self.runtime_password=secrets.token_hex(24)
        self.environment={'AUTH_SECRET':secrets.token_hex(32),'AUTH_TRUST_HOST':'true',
            'AUTH_EMAIL':'release-fixture@example.invalid','AUTH_PASSWORD':secrets.token_hex(24),
            'ENCRYPTION_KEY':secrets.token_hex(32),'UPLOAD_DIR':'/app/uploads'}

    def cleanup(self):
        failed=[]
        for kind,name in reversed(self.made):
            try:
                result=subprocess.run(['docker',kind,'rm',*(['-f'] if kind=='container' else []),name],
                                      capture_output=True,text=True,timeout=30)
                # One-shot --rm containers are expected to be absent already.
                if result.returncode and not (kind=='container' and 'No such container:' in result.stderr):
                    failed.append((kind,name))
            except subprocess.TimeoutExpired:
                failed.append((kind,name))
        require(not failed, 'FIXTURE_CLEANUP_FAILED')

    def name(self, suffix):
        self.serial+=1
        name=self.network+'-'+suffix+'-'+str(self.serial)
        self.made.append(('container',name))
        return name

    def sql(self, statement, database=None):
        return run('docker','exec','-i',self.db,'psql','-v','ON_ERROR_STOP=1','-U','ci_admin',
                   '-d',database or self.database,'-At',input=statement)

    def setup(self):
        run('docker','network','create','--internal',self.network)
        self.made.append(('network',self.network))
        run('docker','volume','create',self.volume)
        self.made.append(('volume',self.volume))
        self.made.append(('container',self.db))
        run('docker','run','-d','--name',self.db,'--network',self.network,'--network-alias','database',
            '--memory','512m','--cpus','1','--tmpfs','/var/lib/postgresql/data:rw',
            '-e','POSTGRES_USER=ci_admin','-e','POSTGRES_PASSWORD='+self.password,
            '-e','POSTGRES_DB='+self.database,POSTGRES)
        for _ in range(60):
            try:
                self.sql('SELECT 1;');break
            except RuntimeError:time.sleep(1)
        else:raise ValueError('DATABASE_NOT_READY')
        self.sql("CREATE ROLE image_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS; "
            "CREATE ROLE image_migrator LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '"+self.migrator_password+"'; "
            "CREATE ROLE image_runtime LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '"+self.runtime_password+"'; "
            'GRANT image_owner TO image_migrator; ALTER DATABASE '+self.database+' OWNER TO image_owner; '
            'ALTER SCHEMA public OWNER TO image_owner; REVOKE ALL ON SCHEMA public FROM PUBLIC; '
            'REVOKE ALL ON DATABASE '+self.database+' FROM PUBLIC; '
            'GRANT CONNECT ON DATABASE '+self.database+' TO image_migrator,image_runtime; '
            'GRANT USAGE ON SCHEMA public TO image_runtime; '
            'ALTER DEFAULT PRIVILEGES FOR ROLE image_owner IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO image_runtime;')
        require(json.loads(run('docker','network','inspect',self.network))[0]['Internal'] is True,
                'NETWORK_NOT_INTERNAL')

    def migration(self, image, mode, database=None):
        database=database or self.database
        url='postgresql://image_migrator:'+self.migrator_password+'@database:5432/'+database+'?options=-c%20role%3Dimage_owner'
        args=['docker','run','--rm','--name',self.name('migration'),'--network',self.network,
            '--read-only','--tmpfs','/tmp:rw,nosuid,size=64m','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--pids-limit','128','--memory','512m','--cpus','1','-e','HOME=/tmp','-e','CHECKPOINT_DISABLE=1',
            '-e','DATABASE_URL='+url,'--entrypoint','node',image]
        if mode=='deploy':
            run(*args,'node_modules/prisma/build/index.js','migrate','deploy',timeout=360)
            self.sql('REVOKE ALL ON public._prisma_migrations FROM image_runtime;',database)
            return None
        proof=json.loads(run(*args,'scripts/check-migrations.mjs','--mode',mode,'--expected-database',database,
            '--expected-user','image_migrator','--expected-owner','image_owner',timeout=360))
        require(proof['ok'] is True and proof['pending']==[] and proof['schema_drift_checked'] is True,
                'MIGRATION_COMPATIBILITY_FAILED')
        return proof

    def migration_suite(self, image):
        database='meeting_migrations_ci_'+self.token
        self.sql('CREATE DATABASE '+database+';')
        proof=json.loads(run('docker','run','--rm','--name',self.name('migration-check'),
            '--network','container:'+self.db,'--read-only','--tmpfs','/tmp:rw,nosuid,size=64m',
            '--cap-drop','ALL','--security-opt','no-new-privileges','--pids-limit','128',
            '--memory','512m','--cpus','1','-e','HOME=/tmp','-e','CHECKPOINT_DISABLE=1',
            '-e','DATABASE_URL=postgresql://ci_admin:'+self.password+'@127.0.0.1:5432/'+database,
            '--entrypoint','node',image,'scripts/qualify-migrations.mjs',timeout=480))
        require(proof['ok'] is True and len(proof['checks'])==15, 'MIGRATION_SUITE_FAILED')
        return proof

    def start(self, image, suffix, database=None, volume=None):
        app=self.name(suffix)
        args=['docker','run','-d','--name',app,'--network',self.network,'--memory','768m','--cpus','1',
            '--security-opt','no-new-privileges','--pids-limit','128','--tmpfs','/run/cloudbox/deploy:rw',
            '--mount','type=volume,source='+(volume or self.volume)+',target=/app/uploads',
            '-e','DATABASE_URL=postgresql://image_runtime:'+self.runtime_password+'@database:5432/'+(database or self.database)]
        for key,value in self.environment.items():args.extend(['-e',key+'='+value])
        run(*args,image)
        run('docker','exec','-u','0',app,'sh','-c',
            'chmod 755 /run/cloudbox/deploy; mkdir /run/cloudbox/deploy/leases; chown 1001:1001 /run/cloudbox/deploy/leases /app/uploads; chmod 700 /run/cloudbox/deploy/leases /app/uploads')
        info=json.loads(run('docker','container','inspect',app))[0]
        require(info['Image']==image and not info['HostConfig']['PortBindings'], 'CONTAINER_IDENTITY_OR_PORT_MISMATCH')
        require(info['HostConfig']['NetworkMode']==self.network, 'CONTAINER_NETWORK_MISMATCH')
        require(any(m.get('Name')==(volume or self.volume) and m['Destination']=='/app/uploads' for m in info['Mounts']),
                'UPLOAD_VOLUME_MISMATCH')
        for _ in range(90):
            try:
                status,body,_=local_request(app,'/api/health')
                if status==200 and json.loads(body)=={'status':'ok'}:return app
            except (OSError,ValueError,RuntimeError):pass
            time.sleep(1)
        raise ValueError('APP_NOT_READY')

    def login(self, app):
        cookies={}
        status,body,_=local_request(app,'/api/auth/csrf',cookies=cookies)
        require(status==200, 'CSRF_FAILED')
        data=urllib.parse.urlencode({'csrfToken':json.loads(body)['csrfToken'],
            'email':self.environment['AUTH_EMAIL'],'password':self.environment['AUTH_PASSWORD'],
            'callbackUrl':'http://127.0.0.1:3003/','json':'true'}).encode()
        require(local_request(app,'/api/auth/callback/credentials',data,cookies)[0]==200, 'LOGIN_FAILED')
        require(any('session-token' in key for key in cookies), 'SESSION_MISSING')
        return cookies,data

    def read(self, app, path, cookies):
        # Never update the saved cookie jar: every stage must accept the original JWT.
        status,body,_=local_request(app,path,cookies=dict(cookies))
        require(status==200, 'AUTHENTICATED_READ_FAILED')
        return json.loads(body)

    def write(self, app, path, cookies, value, method='POST', expected=200):
        status,body,_=local_request(app,path,json.dumps(value).encode(),dict(cookies),
                                   method=method,content_type='application/json')
        require(status==expected, 'AUTHENTICATED_WRITE_FAILED')
        return json.loads(body)

    def version(self, app, metadata, cookies):
        for path in ('/api/version','/api/stats','/api/projects','/api/meetings/maintenance-meeting'):
            require(local_request(app,path)[0]==401, 'ANONYMOUS_AUTH_BOUNDARY_FAILED')
        status,body,headers=local_request(app,'/api/version',cookies=dict(cookies))
        require(status==200 and json.loads(body)=={k:metadata[k] for k in ('version','revision','build_id')},
                'AUTHENTICATED_VERSION_MISMATCH')
        require('no-store' in headers.get('cache-control',''), 'VERSION_CACHE_BOUNDARY_FAILED')
        self.read(app,'/api/stats',cookies)

    def snapshot(self, app, cookies):
        return {path:self.read(app,path,cookies) for path in
                ('/api/projects','/api/meetings/maintenance-meeting','/api/settings')}

    def rows(self, database=None):
        value={table:json.loads(self.sql('SELECT COALESCE(jsonb_agg(v ORDER BY v::text),\'[]\'::jsonb)::text FROM '
               '(SELECT to_jsonb(t) AS v FROM public."'+table+'" t) s;',database)) for table in FIXTURE_TABLES}
        require(all(value.values()), 'SYNTHETIC_TABLE_COVERAGE_MISSING')
        return value

    def files(self, app, contents=None):
        script="""let raw='';process.stdin.on('data',c=>raw+=c);process.stdin.on('end',()=>{
          const fs=require('fs'),x=JSON.parse(raw);if(x)for(const [name,body]of Object.entries(x)){
            if(!/^maintenance-[a-z]+\\.txt$/.test(name))throw Error('name');
            fs.writeFileSync('/app/uploads/'+name,Buffer.from(body,'base64'));}
          const out={};for(const name of fs.readdirSync('/app/uploads').sort()){
            if(!/^maintenance-[a-z]+\\.txt$/.test(name))throw Error('name');
            out[name]=fs.readFileSync('/app/uploads/'+name).toString('base64');}console.log(JSON.stringify(out));});"""
        return json.loads(run('docker','exec','-i',app,'node','-e',script,input=json.dumps(contents)))


def qualify_cross_version(image, baseline_image, baseline_revision, baseline_version='0.1.5',
                          baseline_build_id=None, candidate_revision=None):
    require(re.fullmatch(r'[0-9a-f]{40}',baseline_revision or ''), 'BASELINE_REVISION_REQUIRED')
    baseline=image_metadata(baseline_image,immutable=True,revision=baseline_revision,
                            version=baseline_version,build_id=baseline_build_id)
    candidate=image_metadata(image,revision=candidate_revision)
    require(baseline['image_id']!=candidate['image_id'] and baseline['revision']!=candidate['revision'],
            'DISTINCT_CANDIDATE_REQUIRED')
    return exercise_cross_version(baseline,candidate)


def exercise_cross_version(baseline, candidate):
    """Run the fixture; identical-image developer rehearsals never qualify a release."""
    distinct=baseline['image_id']!=candidate['image_id'] and baseline['revision']!=candidate['revision']
    f=CrossVersionFixture()
    try:
        f.setup()
        migration_proof=f.migration_suite(candidate['image_id'])
        f.migration(baseline['image_id'],'deploy')
        baseline_schema=f.migration(baseline['image_id'],'verify')
        # Maintenance qualifies only unchanged schema/history. Pending migrations
        # fail before candidate deployment, exactly as the protected helper does.
        candidate_preflight=f.migration(candidate['image_id'],'preflight')
        require(candidate_preflight['catalog_sha256']==baseline_schema['catalog_sha256'], 'BASELINE_SCHEMA_MISMATCH')
        app=f.start(baseline['image_id'],'baseline')
        original_session,_=f.login(app)
        f.version(app,baseline,original_session)
        f.sql("""INSERT INTO public."Project" (id,"userId",name,color,"createdAt","updatedAt")
            SELECT 'maintenance-fixture',id,'Maintenance fixture','#71717a',now(),now() FROM public."User";
          INSERT INTO public."Meeting" (id,"userId",title,"projectId","originalFileName","storagePath","fileSize","mimeType",status,"updatedAt")
            SELECT 'maintenance-meeting',id,'Synthetic meeting','maintenance-fixture','maintenance-baseline.txt','/app/uploads/maintenance-baseline.txt',48,'text/plain','COMPLETED',now() FROM public."User";
          INSERT INTO public."Tag" (id,"userId",name,"updatedAt") SELECT 'maintenance-tag',id,'Synthetic tag',now() FROM public."User";
          INSERT INTO public."MeetingTag" ("meetingId","tagId") VALUES ('maintenance-meeting','maintenance-tag');
          INSERT INTO public."Speaker" (id,"meetingId","speakerIndex",label,color) VALUES ('maintenance-speaker','maintenance-meeting',0,'Synthetic speaker','#71717a');
          INSERT INTO public."Transcript" (id,"meetingId","rawResponse","fullText","updatedAt") VALUES ('maintenance-transcript','maintenance-meeting','{}','Synthetic transcript',now());
          INSERT INTO public."Utterance" (id,"transcriptId","speakerId",text,"startTime","endTime",confidence,"orderIndex") VALUES ('maintenance-utterance','maintenance-transcript','maintenance-speaker','Synthetic transcript',0,1,1,0);
          INSERT INTO public."Analysis" (id,"meetingId",summary,topics,"keyPoints","actionItems","rawResponse","updatedAt") VALUES ('maintenance-analysis','maintenance-meeting','Synthetic summary','[]','[]','[]','{}',now());""")
        f.write(app,'/api/settings',original_session,{'key':'GEMINI_API_KEY','value':'synthetic-never-valid-credential-1234'})
        baseline_snapshot=f.snapshot(app,original_session)
        setting=next(s for s in baseline_snapshot['/api/settings']['apiKeys'] if s['key']=='GEMINI_API_KEY')
        require(setting['isConfigured'] and setting['maskedValue'].endswith('1234'), 'SYNTHETIC_ENCRYPTION_FAILED')
        baseline_rows=f.rows()
        baseline_files=f.files(app,{'maintenance-baseline.txt':base64.b64encode(b'Synthetic maintenance upload; no personal data.\n').decode()})
        run('docker','stop',app)
        f.migration(candidate['image_id'],'deploy')
        candidate_schema=f.migration(candidate['image_id'],'verify')
        require(candidate_schema['catalog_sha256']==baseline_schema['catalog_sha256'], 'CANDIDATE_CHANGED_SCHEMA_OR_HISTORY')
        upgraded=f.start(candidate['image_id'],'candidate')
        f.version(upgraded,candidate,original_session)
        require(f.snapshot(upgraded,original_session)==baseline_snapshot and f.rows()==baseline_rows, 'UPGRADE_CHANGED_DATA')
        require(f.files(upgraded)==baseline_files, 'UPGRADE_CHANGED_UPLOADS')
        candidate_session,login_data=f.login(upgraded)
        f.write(upgraded,'/api/meetings/maintenance-meeting',original_session,
                {'title':'Written by candidate','favorite':True},method='PATCH')
        f.write(upgraded,'/api/projects',original_session,{'name':'Candidate project','color':'#123456'},expected=201)
        f.write(upgraded,'/api/settings',original_session,{'key':'GEMINI_API_KEY','value':'synthetic-candidate-never-valid-5678'})
        candidate_snapshot=f.snapshot(upgraded,original_session)
        require(candidate_snapshot['/api/meetings/maintenance-meeting']['title']=='Written by candidate' and
                len(candidate_snapshot['/api/projects'])==2 and
                next(s for s in candidate_snapshot['/api/settings']['apiKeys'] if s['key']=='GEMINI_API_KEY')['maskedValue'].endswith('5678'),
                'CANDIDATE_WRITES_NOT_VISIBLE')
        candidate_rows=f.rows()
        verify_data_transition(baseline_rows,candidate_rows,
            {'Meeting':{'title','favorite','updatedAt'},'Setting':{'value','updatedAt'}},{'Project':1})
        candidate_files=f.files(upgraded,{'maintenance-candidate.txt':base64.b64encode(b'Synthetic candidate upload.\n').decode()})
        require(all(candidate_files.get(name)==data for name,data in baseline_files.items()),
                'CANDIDATE_WRITE_CHANGED_BASELINE_UPLOAD')
        run('docker','exec','-u','0',upgraded,'touch','/run/cloudbox/deploy/draining')
        require(local_request(upgraded,'/api/auth/callback/credentials',login_data,dict(candidate_session))[0]==503, 'DRAIN_LOGIN_NOT_BLOCKED')
        require(local_request(upgraded,'/api/projects',b'{"name":"Must be blocked"}',dict(original_session),content_type='application/json')[0]==503,
                'DRAIN_MUTATION_NOT_BLOCKED')
        require(local_request(upgraded,'/api/health')[0]==200, 'DRAIN_READINESS_FAILED')
        f.version(upgraded,candidate,original_session)
        run('docker','exec','-u','0',upgraded,'rm','/run/cloudbox/deploy/draining')
        require(run('docker','exec',upgraded,'sh','-c','ls -A /run/cloudbox/deploy/leases')=='', 'LEASES_NOT_RELEASED')
        run('docker','stop',upgraded)
        rollback_schema=f.migration(baseline['image_id'],'verify')
        require(rollback_schema['catalog_sha256']==baseline_schema['catalog_sha256'], 'ROLLBACK_SCHEMA_OR_HISTORY_MISMATCH')
        rollback=f.start(baseline['image_id'],'rollback')
        for session in (original_session,candidate_session):
            f.version(rollback,baseline,session)
            require(f.snapshot(rollback,session)==candidate_snapshot, 'ROLLBACK_DATA_OR_SESSION_MISMATCH')
        require(f.rows()==candidate_rows and f.files(rollback)==candidate_files, 'ROLLBACK_DATA_OR_UPLOAD_MISMATCH')
        f.write(rollback,'/api/meetings/maintenance-meeting',candidate_session,{'description':'Written after rollback'},method='PATCH')
        final_snapshot=f.snapshot(rollback,original_session)
        require(final_snapshot['/api/meetings/maintenance-meeting']['description']=='Written after rollback', 'ROLLBACK_WRITE_FAILED')
        final_rows=f.rows()
        verify_data_transition(candidate_rows,final_rows,{'Meeting':{'description','updatedAt'}})
        # Restore post-candidate data into an additional empty database and volume;
        # the rollback app/database remain intact and must still read identically.
        dump=run('docker','exec',f.db,'pg_dump','-U','ci_admin','-d',f.database,'--no-owner','--no-acl',timeout=60)
        require(len(dump.encode())<4*1024*1024 and 'maintenance-meeting' in dump, 'INVALID_SYNTHETIC_DUMP')
        restored_database='meeting_restore_ci_'+f.token
        f.sql('CREATE DATABASE '+restored_database+' OWNER image_owner;')
        f.sql('ALTER SCHEMA public OWNER TO image_owner; REVOKE ALL ON SCHEMA public FROM PUBLIC;',restored_database)
        f.sql('SET ROLE image_owner;\n'+dump,restored_database)
        f.sql('GRANT CONNECT ON DATABASE '+restored_database+' TO image_runtime,image_migrator; GRANT USAGE ON SCHEMA public TO image_runtime; '
              'GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO image_runtime; '
              'REVOKE ALL ON public._prisma_migrations FROM image_runtime;',restored_database)
        recovered_schema=f.migration(baseline['image_id'],'verify',restored_database)
        require(recovered_schema['applied']==baseline_schema['applied'], 'RECOVERY_MIGRATION_HISTORY_MISMATCH')
        restored_volume=f.network+'-restored-uploads'
        run('docker','volume','create',restored_volume)
        f.made.append(('volume',restored_volume))
        recovered=f.start(baseline['image_id'],'recovery',restored_database,restored_volume)
        require(f.files(recovered,candidate_files)==candidate_files, 'RECOVERED_UPLOAD_MISMATCH')
        for session in (original_session,candidate_session):
            f.version(recovered,baseline,session)
            require(f.snapshot(recovered,session)==final_snapshot, 'RECOVERY_DATA_OR_SESSION_MISMATCH')
        require(f.rows(restored_database)==final_rows and f.rows()==final_rows and
                f.snapshot(rollback,original_session)==final_snapshot and f.files(rollback)==candidate_files,
                'RECOVERY_CHANGED_ORIGINAL_DATA')
        hashes={name:hashlib.sha256(base64.b64decode(data,validate=True)).hexdigest() for name,data in candidate_files.items()}
        return {'schema_version':2,'ok':True,'harness_rehearsal_only':not distinct,
            'image_id':candidate['image_id'],'version':candidate['version'],
            'candidate_revision':candidate['revision'],'candidate_build_id':candidate['build_id'],
            'candidate_build_timestamp':candidate['build_timestamp'],
            **{'baseline_'+key:value for key,value in baseline.items()},
            'observed_at':datetime.now(timezone.utc).isoformat(),
            'migration_checks':migration_proof['checks'],'migration_readonly_image':True,
            'checks':['readiness_database','anonymous_auth_boundary','fixture_login','authenticated_version',
                      'runtime_database_stats','drain_blocks_mutations','leases_released'],
            'external_processing':False,'production_restore':False,'network_internal':True,'ports_published':False,'model_calls':0,
            'synthetic_restore':{'database_separate':True,'authenticated_projects':True,'existing_session_preserved':True,
                'candidate_session_preserved':True,'original_application_preserved':True,'all_fixture_tables_preserved':True,
                'schema_verified':True,'migration_history_verified':True,
                'upload_sha256':hashes['maintenance-baseline.txt'],'restored_upload_sha256':hashes['maintenance-baseline.txt'],
                'upload_hashes':hashes,'restored_upload_hashes':hashes},
            'cross_version':{'sequence':['baseline','candidate','baseline'],'baseline':baseline,'candidate':candidate,
                'checks':{key:True for key in CROSS_VERSION_CHECKS},'fixture_tables':list(FIXTURE_TABLES),
                'catalog_sha256':baseline_schema['catalog_sha256'],'upload_hashes':hashes,
                'data_after_candidate_sha256':hashlib.sha256(json.dumps(candidate_rows,sort_keys=True).encode()).hexdigest()},
            'candidate_upgrade_and_downgrade_qualified':distinct}
    finally:
        f.cleanup()


def parse_args(args):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('--baseline-image')
    parser.add_argument('--baseline-revision')
    parser.add_argument('--baseline-version',default='0.1.5')
    parser.add_argument('--baseline-build-id')
    parser.add_argument('--candidate-revision')
    result=parser.parse_args(args)
    if bool(result.baseline_image)!=bool(result.baseline_revision):
        parser.error('--baseline-image and --baseline-revision must be supplied together')
    if not result.baseline_image and (result.baseline_build_id or result.candidate_revision):
        parser.error('cross-version metadata requires --baseline-image')
    return result


if __name__=='__main__':
    args=parse_args(sys.argv[1:])
    try:
        if args.baseline_image:
            print(json.dumps(qualify_cross_version(**vars(args))))
        else:
            qualify(args.image)
    except Exception as error:
        # Docker/Prisma failures can include temporary credentials in arguments.
        code=str(error) if isinstance(error,ValueError) and re.fullmatch(r'[A-Z_]{3,80}',str(error)) else 'IMAGE_QUALIFICATION_FAILED'
        print(json.dumps({'ok':False,'error':code}),file=sys.stderr)
        sys.exit(1)
