#!/usr/bin/env python3
"""Personal adoption handoff inside Deploy; reuse original prepared image, never D1.

The operator must install/read back the exact root fence before dispatch. The
ordinary transport does not prove that an absent fence exists. Disabled in Git.
"""
from datetime import datetime,timedelta,timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('meeting_release_proof',HERE/'maintenance-release-proof.py')
n=importlib.util.module_from_spec(spec);spec.loader.exec_module(n)
r=n.r;g=n.gate;require=g.require
BASE='99f2069dab3c652620e6ee7a48951c67009aa5e9'
NODE='node:26-alpine@sha256:dbaa92e5758cbbcf85d65d5403fdb530fe3442cbe8c6dbfb7ef23365450d5070'
KEYS={'schema_version','service','source_sha','source_tree','baseline_receipt_sha256','target_manifest_sha256','prepare_run_id','prepare_run_attempt','cumulative_delta_sha256','issued_at','expires_at'}
# Exact reviewed adoption paths. Runtime handlers, schema/migrations and content
# are absent; this list never relaxes monthly dependency classification.
ALLOWED={'.github/dependabot.yml','.github/maintenance.json','.github/bootstrap.json','.github/workflows/ci.yml',
'.github/workflows/deploy.yml','.github/workflows/maintenance.yml','AGENTS.md','package-lock.json','package.json',
'renovate.json','docker/Dockerfile','scripts/maintenance-guard.py','scripts/maintenance-pipeline.py',
'scripts/maintenance-receiver.py','scripts/maintenance-release-proof.py','scripts/maintenance-semver.cjs',
'scripts/maintenance-source-gate.py','scripts/maintenance-source-proof.py','scripts/maintenance-tools/.gitignore',
'scripts/maintenance-tools/Dockerfile','scripts/maintenance-tools/package-lock.json','scripts/maintenance-tools/package.json',
'scripts/publish-release.py','scripts/qualify-image.py','scripts/reviewed-bootstrap.py',
 'tests/dependency-security.test.mjs','tests/maintenance-eslint-compat.test.mjs','tests/test_image_qualification.py',
 'tests/test_maintenance_pipeline.py','tests/test_maintenance_proof.py','tests/test_maintenance_receiver.py',
 'tests/test_maintenance_release_proof.py','tests/test_maintenance_source.py','tests/test_publish_release.py',
 'tests/test_release_maintenance.py','tests/test_reviewed_bootstrap.py'}


def clock():return datetime.now(timezone.utc)


def envelope(request,config,env,event,now):
    r.exact(config,{'schema_version','service','enabled','baseline_commit'},'BOOTSTRAP_CONFIG')
    require(type(config['schema_version']) is int and config['enabled'] is True and config=={'schema_version':1,'service':'meeting-ai','enabled':True,'baseline_commit':BASE},'BOOTSTRAP_DISABLED')
    r.exact(request,KEYS,'BOOTSTRAP_SCHEMA')
    require(type(request['schema_version']) is int and request['schema_version']==1 and request['service']=='meeting-ai','BOOTSTRAP_SERVICE')
    for k in ('source_sha','source_tree'):require(isinstance(request[k],str) and g.SHA.fullmatch(request[k]),'BOOTSTRAP_SOURCE')
    for k in ('baseline_receipt_sha256','target_manifest_sha256','cumulative_delta_sha256'):
        require(isinstance(request[k],str) and r.HASH.fullmatch(request[k]),'BOOTSTRAP_HASH')
    for k in ('prepare_run_id','prepare_run_attempt'):require(type(request[k]) is int and 0<request[k]<2**53,'BOOTSTRAP_RUN')
    issued,expiry=r.source.stamp(request['issued_at']),r.source.stamp(request['expires_at'])
    require(issued<=now<expiry and timedelta(0)<expiry-issued<=timedelta(hours=1) and expiry-now>=timedelta(minutes=15),'BOOTSTRAP_TTL')
    require(env.get('GITHUB_REPOSITORY')==r.REPO and env.get('GITHUB_REPOSITORY_ID')==str(r.source.REPOSITORY_ID)
        and env.get('GITHUB_REF')=='refs/heads/main' and env.get('GITHUB_EVENT_NAME')=='workflow_dispatch'
        and env.get('GITHUB_RUN_ATTEMPT')=='1' and env.get('GITHUB_ACTOR_ID')=='22529012'
        and env.get('GITHUB_ACTOR')==env.get('GITHUB_TRIGGERING_ACTOR')=='leodots'
        and event.get('sender',{}).get('id')==22529012,'BOOTSTRAP_PERSONAL_CALLER')
    require(env.get('GITHUB_SHA')==env.get('GITHUB_WORKFLOW_SHA')==request['source_sha'] and
        env.get('GITHUB_WORKFLOW_REF')==r.REPO+'/.github/workflows/deploy.yml@refs/heads/main','BOOTSTRAP_WORKFLOW')
    require(event.get('inputs',{}).get('prepare_only')=='false','BOOTSTRAP_NOT_PREPARATION')


def delta(before,after):
    g.validate_snapshot(before);g.validate_snapshot(after);require(before['commit']==BASE,'BOOTSTRAP_BASELINE')
    rows=[{'path':p,'before':before['files'].get(p),'after':after['files'].get(p)} for p in sorted(before['files'].keys()|after['files'].keys()) if before['files'].get(p)!=after['files'].get(p)]
    require(rows and {x['path'] for x in rows}<=ALLOWED,'BOOTSTRAP_UNREVIEWED_PATH')
    old,new=(g.decode(s['contents']['package.json']) for s in (before,after))
    expected=g.decode(g.canonical(old).decode());expected['overrides']['@prisma/config']={'deepmerge-ts':'8.0.0'}
    require(new==expected,'BOOTSTRAP_PACKAGE_BEHAVIOR')
    oldlock,newlock=(g.decode(s['contents']['package-lock.json']) for s in (before,after))
    require(oldlock.get('lockfileVersion')==newlock.get('lockfileVersion')==3,'BOOTSTRAP_LOCK_VERSION')
    require({k:v for k,v in oldlock.items() if k!='packages'}=={k:v for k,v in newlock.items() if k!='packages'},'BOOTSTRAP_LOCK_METADATA')
    a,b=oldlock['packages'],newlock['packages'];path='node_modules/deepmerge-ts'
    require(a.keys()==b.keys() and {p for p in a if a[p]!=b[p]}=={path},'BOOTSTRAP_LOCK_SCOPE')
    previous,current=a[path],b[path]
    require(previous['version']=='7.1.5' and current['version']=='8.0.0' and
        current['resolved']=='https://registry.npmjs.org/deepmerge-ts/-/deepmerge-ts-8.0.0.tgz' and
        current['integrity']=='sha512-ICNjaP0ML+eSdEpJYQC46XiAn/UjAdwbEl0dE8p85ZTeNDinN4Kd4+9jS4OSAuH7st6eC7rQhsqTF5zIDaUm2g==' and
        {k:v for k,v in previous.items() if k not in {'version','resolved','integrity','funding'}}==
        {k:v for k,v in current.items() if k not in {'version','resolved','integrity','funding'}},'BOOTSTRAP_REVIEWED_OVERRIDE')
    return {'baseline_commit':BASE,'source_commit':after['commit'],'source_tree':after['tree'],'files':rows,
        'dependency':{'name':'deepmerge-ts','before':'7.1.5','version':'8.0.0','integrity':current['integrity']}}


def artifact(api,run,attempt,name,files):
    listing=api.get(r.PREFIX+f'/actions/runs/{run}/artifacts?per_page=100')
    require(type(listing.get('total_count')) is int and listing['total_count']==len(listing.get('artifacts',[]))<100,'BOOTSTRAP_ARTIFACT_LIST')
    found=[m for m in listing['artifacts'] if m.get('name')==name]
    require(len(found)==1 and found[0].get('expired') is False,'BOOTSTRAP_ARTIFACT_MISSING')
    m=found[0];linked=m.get('workflow_run',{})
    require(linked.get('id')==run and linked.get('repository_id')==linked.get('head_repository_id')==r.source.REPOSITORY_ID,'BOOTSTRAP_ARTIFACT_ORIGIN')
    raw=api.artifact_bytes(m)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        require(set(z.namelist())==set(files) and len(z.namelist())==len(files),'BOOTSTRAP_ARTIFACT_FILES')
        require(all(0<i.file_size<=128*1024 and not i.flag_bits&1 for i in z.infolist()) and sum(i.file_size for i in z.infolist())<=256*1024,'BOOTSTRAP_ARTIFACT_SIZE')
        return {name:z.read(name) for name in files},linked


def latest_ci(api,sha,now):
    listing=api.get(r.PREFIX+'/actions/workflows/ci.yml/runs?head_sha='+sha+'&per_page=30')
    runs=listing.get('workflow_runs')
    require(isinstance(runs,list) and type(listing.get('total_count')) is int and 0<listing['total_count']==len(runs)<30,'BOOTSTRAP_CI_LIST')
    require(all(type(row.get('id')) is int and row['id']>0 and type(row.get('run_attempt')) is int and row['run_attempt']>0 for row in runs) and len({row['id'] for row in runs})==len(runs),'BOOTSTRAP_CI_LIST')
    require(all(row.get('status')=='completed' for row in runs),'BOOTSTRAP_CI_PENDING')
    stamps=[]
    for row in runs:
        require(isinstance(row.get('run_started_at'),str),'BOOTSTRAP_CI_TIMESTAMP')
        stamp=r.source.stamp(row['run_started_at'])
        require(stamp<=now,'BOOTSTRAP_CI_TIMESTAMP');stamps.append(stamp)
    latest=max(stamps)
    require(stamps.count(latest)==1,'BOOTSTRAP_CI_AMBIGUOUS')
    return runs[stamps.index(latest)]


def authenticate(request,config,env,event,api,root,*,now_fn=clock):
    started=now_fn();envelope(request,config,env,event,started);sha=request['source_sha'];rid=request['prepare_run_id'];attempt=request['prepare_run_attempt']
    require(api.get(r.PREFIX+'/commits/main').get('sha')==sha,'BOOTSTRAP_MAIN_DRIFT')
    latest=latest_ci(api,sha,now_fn())
    require(latest.get('head_sha')==sha and latest.get('path')=='.github/workflows/ci.yml' and latest.get('event')=='push' and latest.get('status')=='completed' and latest.get('conclusion')=='success' and latest.get('head_repository',{}).get('id')==r.source.REPOSITORY_ID,'BOOTSTRAP_CI_REQUIRED')
    baseline=r.source.published_baseline(api,root,include_manifest=True)['manifest']
    require(baseline['git_sha']==BASE and g.sha256(baseline)==request['baseline_receipt_sha256'],'BOOTSTRAP_BASELINE_DRIFT')
    review=delta(g.collect(root,BASE),g.collect(root,sha))
    require(review['source_tree']==request['source_tree'] and g.sha256(review)==request['cumulative_delta_sha256'],'BOOTSTRAP_DELTA_DRIFT')
    docker=subprocess.check_output(['git','--no-replace-objects','-C',str(root),'show',sha+':docker/Dockerfile'],text=True,timeout=15)
    require([line.split()[1] for line in docker.splitlines() if line.startswith('FROM ')]==[NODE,'base','base','base'],'BOOTSTRAP_BASE_IMAGE')
    run=api.get(r.PREFIX+f'/actions/runs/{rid}')
    require(run.get('id')==rid and run.get('run_attempt')==attempt and run.get('head_sha')==sha and run.get('path')=='.github/workflows/deploy.yml' and
        run.get('status')=='completed' and run.get('conclusion')=='success' and run.get('event')=='workflow_dispatch' and
        run.get('actor',{}).get('id')==run.get('triggering_actor',{}).get('id')==22529012,'BOOTSTRAP_PREPARE_RUN')
    jobs=api.get(r.PREFIX+f'/actions/runs/{rid}/attempts/{attempt}/jobs?per_page=100')
    require(type(jobs.get('total_count')) is int and jobs['total_count']==len(jobs.get('jobs',[]))<=10 and
        {j['name'] for j in jobs['jobs'] if j.get('conclusion')=='success'}=={'Select verified source and version','Build and qualify exact image'} and
        all(j.get('run_id')==rid and j.get('status')=='completed' and j.get('conclusion') in ('success','skipped') for j in jobs['jobs']),'BOOTSTRAP_PREPARE_JOBS')
    wanted=[(f'meeting-release-{rid}-{attempt}',{'release.json','release-image.json'}),
        (f'meeting-ai-preparation-{rid}-{attempt}',{'preparation.json'}),
        (f'meeting-ai-release-functional-{rid}-{attempt}',{'meeting-functional.json'}),
        (f'meeting-ai-release-security-{rid}-{attempt}',{s+'/security.json' for s in ('runtime','builder','deps')})]
    collected={}
    for name,files in wanted:
        result,linked=artifact(api,rid,attempt,name,files);require(linked.get('head_sha')==sha,'BOOTSTRAP_ARTIFACT_SHA')
        for filename,raw in result.items():collected[('security/' if name.startswith('meeting-ai-release-security-') else '')+filename]=raw
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        for name,raw in collected.items():
            path=Path(temp)/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        manifest,image,functional,_,scans=n.validate_inputs(Path(temp),{'GITHUB_RUN_ID':str(rid),'GITHUB_RUN_ATTEMPT':str(attempt)},now_fn())
    r.exact(g.decode(collected['preparation.json'].decode()),{'schema_version','prepare_only','run_id','run_attempt','source_sha','manifest_sha256'},'BOOTSTRAP_PREPARATION_SCHEMA')
    require(g.decode(collected['preparation.json'].decode())=={'schema_version':1,'prepare_only':True,'run_id':rid,'run_attempt':attempt,'source_sha':sha,'manifest_sha256':request['target_manifest_sha256']},'BOOTSTRAP_PREPARATION_FLAG')
    sys.path.insert(0,str(HERE));from release_manifest import validate
    manifest=validate(manifest)
    require(manifest['service']==manifest['compose_project']=='meeting-ai' and manifest['source_repository']=='https://github.com/'+r.REPO and
        manifest['git_sha']==sha and manifest['deployment']['status']=='built' and manifest['app_version']==manifest['release_version'] and
        manifest['build']['id']==str(rid) and manifest['build']['attempt']==attempt and
        re.fullmatch(r'ghcr.io/leodotsinc/meeting-ai@sha256:[a-f0-9]{64}',manifest['image']) and
        g.sha256(manifest)==request['target_manifest_sha256'],'BOOTSTRAP_MANIFEST')
    package=api.package('deepmerge-ts');version=package.get('versions',{}).get('8.0.0',{})
    require(package.get('name')==version.get('name')=='deepmerge-ts' and version.get('version')=='8.0.0' and
        version.get('dist',{}).get('integrity')==review['dependency']['integrity'] and not version.get('deprecated') and
        now_fn()-r.source.stamp(package['time']['8.0.0'])>=timedelta(days=14),'BOOTSTRAP_REGISTRY')
    lock=g.decode(g.collect(root,sha)['contents']['package-lock.json'])
    packages={}
    for path,entry in lock['packages'].items():
        if path:packages.setdefault(g.package_name(path),set()).add(entry['version'])
    require(api.read('https://registry.npmjs.org/-/npm/v1/security/advisories/bulk',payload={k:sorted(v) for k,v in packages.items()})=={},'BOOTSTRAP_OFFICIAL_ADVISORY')
    name='meeting-ai-bootstrap-attempt-'+request['target_manifest_sha256']
    listing=api.get(r.PREFIX+'/actions/artifacts?name='+name+'&per_page=100')
    require(listing.get('total_count')==0 and listing.get('artifacts')==[],'BOOTSTRAP_ATTEMPT_RECONCILIATION')
    require(api.get(r.PREFIX+'/commits/main').get('sha')==sha,'BOOTSTRAP_FINAL_MAIN_DRIFT')
    final_prepare=api.get(r.PREFIX+f'/actions/runs/{rid}')
    require(all(final_prepare.get(k)==run.get(k) for k in ('id','head_sha','run_attempt','status','conclusion')),'BOOTSTRAP_PREPARE_RERUN')
    final_selection=latest_ci(api,sha,now_fn())
    require(all(final_selection.get(k)==latest.get(k) for k in ('id','head_sha','run_attempt','run_started_at','status','conclusion')),'BOOTSTRAP_CI_RERUN')
    final_ci=api.get(r.PREFIX+'/actions/runs/'+str(latest['id']))
    require(all(final_ci.get(k)==latest.get(k) for k in ('id','head_sha','run_attempt','status','conclusion')),'BOOTSTRAP_CI_RERUN')
    envelope(request,config,env,event,now_fn());require(now_fn()-started<=timedelta(minutes=5),'BOOTSTRAP_METADATA_EXPIRED')
    for scan in scans:
        r.fresh(scan['observed_at'],now_fn(),timedelta(hours=24));r.fresh(scan['database_updated_at'],now_fn(),timedelta(hours=24))
    r.fresh(functional['observed_at'],now_fn(),timedelta(hours=24))
    return collected['release.json'],{'schema_version':1,'service':'meeting-ai','source_sha':sha,'image':manifest['image'],
        'image_id':image['image_id'],'version':manifest['release_version'],'manifest_sha256':request['target_manifest_sha256'],
        'prepare_run_id':rid,'prepare_run_attempt':attempt,'production_authorized':False}


def main():
    command=sys.argv[1];root=HERE.parent
    if command=='review':
        value=delta(g.collect(root,BASE),g.collect(root,os.environ['GITHUB_SHA']))
        print(json.dumps({'review':value,'cumulative_delta_sha256':g.sha256(value)},sort_keys=True));return
    require(command in ('validate','image'),'BOOTSTRAP_COMMAND')
    raw=os.environ.get('BOOTSTRAP_REVIEW','');require(0<len(raw.encode())<=16384,'BOOTSTRAP_INPUT_SIZE')
    request=g.decode(raw);config=g.decode((root/'.github/bootstrap.json').read_text());event=g.decode(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    envelope(request,config,os.environ,event,clock())
    if command=='image':
        proof=g.decode(Path('bootstrap-proof.json').read_text())
        require(proof['manifest_sha256']==request['target_manifest_sha256']==g.sha256(g.decode(Path('release.json').read_text())) and os.environ.get('PULLED_IMAGE_ID')==proof['image_id'],'BOOTSTRAP_IMAGE_ID')
        return
    raw,proof=authenticate(request,config,os.environ,event,r.API(os.environ.get('GH_TOKEN')),root)
    Path('release.json').write_bytes(raw);Path('bootstrap-proof.json').write_bytes(g.canonical(proof))
    with open(os.environ['GITHUB_OUTPUT'],'a') as stream:
        for key,value in {'sha':proof['source_sha'],'version':proof['version'],'image':proof['image'],'manifest_sha256':proof['manifest_sha256'],'artifact':'meeting-reviewed-bootstrap-release'}.items():stream.write(key+'='+value+'\n')


if __name__=='__main__':
    try:main()
    except Exception:
        print('REVIEWED_BOOTSTRAP_REFUSED; no host authorization created',file=sys.stderr);raise SystemExit(2)
