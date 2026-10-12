#!/usr/bin/env python3
"""Authenticate a monthly preparation request and existing CI proof; no mutation.

The pipeline module uses this read-only admission before its single merge and
existing Deploy handoff. This module grants no host authority. The source graph
is classified once in isolated CI; this consumer authenticates that proof and
refreshes official metadata/advisories, never runs candidate code or a new parser.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import sys
import tempfile
import zipfile
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('source_proof', HERE/'maintenance-source-proof.py')
source = importlib.util.module_from_spec(spec); spec.loader.exec_module(source)
gate = source.gate; require = gate.require; exact = source.exact
REPO = source.REPO; PREFIX = source.PREFIX
HASH = re.compile(r'[0-9a-f]{64}\Z')
CONFIG_KEYS = {'schema_version','service','enabled','scheduler_app_id','actor_id','sender_id','repository_id',
    'renovate_actor_id','policy_sha256','host_qualification_sha256','host_contract_sha256','minimum_release_age_days','trusted_code','window'}
REQUEST_KEYS = {'schema_version','service','phase','request_id','issued_at','expires_at','policy_sha256',
    'window','source_pr','baseline','base_manifest','infra_commit','config_sha256','host_contract_sha256','source_proof','policy'}
JOBS = {'Dependency source proof','Tests, migrations and build','Exact image, authentication and safe migration bootstrap'}


def fresh(value, now, age):
    require(timedelta(0) <= now-source.stamp(value) <= age, 'STALE_OR_FUTURE_PROOF')


def validate_request(request, config, env, event, now):
    exact(config, CONFIG_KEYS, 'CONFIG_SCHEMA'); exact(request, REQUEST_KEYS, 'REQUEST_SCHEMA')
    require(type(config['schema_version']) is int and config['schema_version']==1 and config['service']=='meeting-ai'
        and type(config['enabled']) is bool, 'CONFIG_IDENTITY')
    require(config['enabled'] is True and isinstance(config['host_qualification_sha256'],str)
        and HASH.fullmatch(config['host_qualification_sha256']), 'RECEIVER_NOT_QUALIFIED')
    require(config['scheduler_app_id']=='5019669' and config['actor_id']==config['sender_id']=='332011818'
        and config['repository_id']==source.REPOSITORY_ID and type(config['repository_id']) is int
        and config['renovate_actor_id']=='29139614' and config['minimum_release_age_days']==14, 'CONFIG_AUTHORITY')
    require(env.get('GITHUB_REPOSITORY')==REPO and env.get('GITHUB_REF')=='refs/heads/main'
        and env.get('GITHUB_EVENT_NAME')=='workflow_dispatch' and env.get('GITHUB_RUN_ATTEMPT')=='1'
        and env.get('GITHUB_ACTOR_ID')=='332011818' and env.get('GITHUB_ACTOR')=='cloudbox-maintenance-scheduler[bot]'
        and env.get('GITHUB_TRIGGERING_ACTOR')==env['GITHUB_ACTOR']
        and event.get('sender',{}).get('id')==332011818 and event['sender'].get('type')=='Bot', 'SCHEDULER_IDENTITY')
    require(type(request['schema_version']) is int and request['schema_version']==1
        and request['service']=='meeting-ai' and request['phase']=='prepare', 'PREPARATION_ONLY')
    require(isinstance(config['host_contract_sha256'],str) and HASH.fullmatch(config['host_contract_sha256'])
        and request['host_contract_sha256']==config['host_contract_sha256'], 'HOST_CONTRACT_NOT_REGISTERED')
    require(isinstance(request['infra_commit'], str) and gate.SHA.fullmatch(request['infra_commit'])
        and request['config_sha256']==gate.sha256(config), 'INFRA_OR_CONFIG_REFERENCE')
    exact(request['source_proof'], {'run_id','run_attempt','artifact_id','digest'}, 'SOURCE_PROOF_REFERENCE')
    reference=request['source_proof']
    require(all(type(reference[k]) is int and reference[k]>0 for k in ('run_id','run_attempt','artifact_id'))
        and isinstance(reference['digest'],str) and re.fullmatch(r'sha256:[0-9a-f]{64}',reference['digest']), 'SOURCE_PROOF_REFERENCE')
    exact(request['source_pr'], {'number','base_sha','head_sha','tree_sha'}, 'PR_SCHEMA')
    pr=request['source_pr']
    require(type(pr['number']) is int and 0<pr['number']<1000000 and
        all(isinstance(pr[k],str) and gate.SHA.fullmatch(pr[k]) for k in ('base_sha','head_sha','tree_sha'))
        and pr['base_sha']==env.get('GITHUB_SHA'), 'PR_IDENTITY')
    exact(request['baseline'], {'git_sha','image','receipt_sha256'}, 'BASELINE_SCHEMA')
    sys.path.insert(0,str(HERE)); from release_manifest import validate
    manifest=validate(request['base_manifest']); baseline=request['baseline']
    require(manifest['service']==manifest['compose_project']=='meeting-ai' and
        manifest['source_repository']=='https://github.com/'+REPO and manifest['application_kind']=='first_party'
        and manifest['app_version']==manifest['release_version'] and manifest['deployment']['status']=='verified'
        and manifest['deployment']['observed_image']==manifest['image'] and
        re.fullmatch(r'ghcr.io/leodotsinc/meeting-ai@sha256:[0-9a-f]{64}',manifest['image']) and
        baseline=={'git_sha':manifest['git_sha'],'image':manifest['image'],'receipt_sha256':gate.sha256(manifest)}, 'BASELINE_BINDING')
    identity={'app':'meeting-ai','baseline_receipt_sha256':baseline['receipt_sha256'],
              'head_sha':pr['head_sha'],'tree_sha':pr['tree_sha']}
    require(isinstance(request['policy'],dict) and gate.sha256(request['policy'])==request['policy_sha256'], 'POLICY_CONTENT_HASH')
    require(request['request_id']==gate.sha256(identity) and isinstance(request['policy_sha256'],str)
        and HASH.fullmatch(request['policy_sha256']) and request['policy_sha256']==config['policy_sha256'], 'REQUEST_OR_POLICY_HASH')
    exact(config['trusted_code'], source.CODE, 'CODE_PINS_REQUIRED')
    require(all(isinstance(v,str) and HASH.fullmatch(v) for v in config['trusted_code'].values()), 'CODE_PIN_INVALID')
    require(config['window']=={'timezone':'America/Sao_Paulo','day':1,'weekday':'first_saturday',
        'start_hour':10,'end_hour':12,'approved':True}, 'APPROVED_WINDOW_REQUIRED')
    exact(request['window'], {'timezone','start','end'}, 'WINDOW_SCHEMA')
    local=now.astimezone(ZoneInfo('America/Sao_Paulo')); start=local.replace(hour=10,minute=0,second=0,microsecond=0)
    end=start+timedelta(hours=2); issued=source.stamp(request['issued_at']); expires=source.stamp(request['expires_at'])
    require(local.weekday()==5 and local.day<=7 and start<=issued<=now<expires<=end and
        expires-issued<=timedelta(hours=1) and request['window']['timezone']=='America/Sao_Paulo'
        and source.stamp(request['window']['start'])==start and source.stamp(request['window']['end'])==end, 'WINDOW_OR_TTL')


def policy_change(request,change,now):
    policy=request['policy'];qualification=policy.get('qualification');normal=policy.get('normal',{})
    require(policy.get('id')=='meeting-ai' and policy.get('kind')=='first_party' and isinstance(qualification,dict)
        and qualification.get('runtime_pilot') is True and qualification.get('scope','dependency_updates')=='dependency_updates'
        and (policy.get('coverage')=='auto_qualified' or policy.get('coverage')=='review_required' and qualification.get('scope')=='dependency_updates')
        and source.stamp(qualification['valid_until'])>now and isinstance(policy.get('review_on'),str)
        and policy['review_on']>=now.date().isoformat(),'POLICY_EXECUTION_SCOPE')
    allowed=normal.get('allowed_changes')
    require(isinstance(allowed,list) and allowed and all(v in ('patch','minor','digest','lockfile') for v in allowed)
        and change in ('patch','minor') and change in allowed,'CHANGE_NOT_AUTHORIZED')


class API(source.API):
    def artifact_bytes(self, metadata):
        identity=metadata.get('id'); size=metadata.get('size_in_bytes'); digest=metadata.get('digest')
        require(type(identity) is int and identity>0 and type(size) is int and 0<size<=256*1024
            and isinstance(digest,str) and re.fullmatch(r'sha256:[0-9a-f]{64}',digest), 'ARTIFACT_METADATA')
        self.calls+=1; require(self.calls<=100 and source.time.monotonic()<self.deadline, 'API_BUDGET')
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'GH_HOST':'github.com','GH_TOKEN':self.token}
        with tempfile.TemporaryDirectory(prefix='meeting-proof-') as folder, tempfile.TemporaryFile() as out:
            env.update(HOME=folder,GH_CONFIG_DIR=folder+'/config')
            def limit():resource.setrlimit(resource.RLIMIT_FSIZE,(256*1024,256*1024))
            result=subprocess.run(['gh','api',PREFIX+'/actions/artifacts/'+str(identity)+'/zip'],
                env=env,stdout=out,stderr=subprocess.DEVNULL,timeout=20,preexec_fn=limit)
            out.seek(0); raw=out.read(256*1024+1)
        require(result.returncode==0 and len(raw)==size and 'sha256:'+hashlib.sha256(raw).hexdigest()==digest,
                'ARTIFACT_BYTES_OR_DIGEST')
        return raw


def tree(api, sha):
    commit=api.get(PREFIX+'/git/commits/'+sha); require(commit.get('sha')==sha, 'COMMIT_IDENTITY')
    tree_sha=commit.get('tree',{}).get('sha'); require(isinstance(tree_sha,str) and gate.SHA.fullmatch(tree_sha), 'TREE_IDENTITY')
    listing=api.get(PREFIX+'/git/trees/'+tree_sha+'?recursive=1')
    require(listing.get('sha')==tree_sha and listing.get('truncated') is False and isinstance(listing.get('tree'),list)
        and len(listing['tree'])<=20000,'TREE_INCOMPLETE')
    files={}
    for row in listing['tree']:
        if row.get('type')=='tree':continue
        require(row.get('type')=='blob' and row.get('path') not in files,'TREE_ENTRY')
        files[row['path']]={'mode':row['mode'],'oid':row['sha']}
    require(gate.tree_oid(files)==tree_sha,'TREE_OBJECT_MISMATCH')
    return {'tree':tree_sha,'files':files}


def ci_proof(api, request, config, now):
    head=request['source_pr']['head_sha']
    listing=api.get(PREFIX+'/actions/workflows/ci.yml/runs?head_sha='+head+'&per_page=30')
    rows=listing.get('workflow_runs'); require(isinstance(rows,list) and type(listing.get('total_count')) is int
        and len(rows)==listing['total_count'] and 0<len(rows)<30,'CI_LIST_INCOMPLETE')
    run=max(rows,key=lambda r:r['id']); run_id=run['id']; attempt=run['run_attempt']
    require(type(run_id) is int and run_id>0 and type(attempt) is int and attempt>0 and
        run.get('head_sha')==head and run.get('event')=='pull_request' and run.get('status')=='completed'
        and run.get('conclusion')=='success' and run.get('path')=='.github/workflows/ci.yml'
        and run.get('head_repository',{}).get('id')==source.REPOSITORY_ID, 'EXACT_CI_REQUIRED')
    require(source.stamp(run['updated_at'])<=now, 'FUTURE_SOURCE_PROOF')
    jobs=api.get(PREFIX+f'/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100')
    require(jobs.get('total_count')==len(JOBS) and isinstance(jobs.get('jobs'),list) and len(jobs['jobs'])==len(JOBS)
        and {j.get('name') for j in jobs['jobs']}==JOBS and all(j.get('status')=='completed'
        and j.get('conclusion')=='success' and j.get('run_id')==run_id and j.get('run_attempt')==attempt
        and j.get('head_sha')==head for j in jobs['jobs']), 'CI_JOBS_REQUIRED')
    artifacts=api.get(PREFIX+f'/actions/runs/{run_id}/artifacts?per_page=100')
    require(type(artifacts.get('total_count')) is int and artifacts['total_count']<=100 and
        isinstance(artifacts.get('artifacts'),list) and len(artifacts['artifacts'])==artifacts['total_count'], 'ARTIFACT_LIST_INCOMPLETE')
    found=[m for m in artifacts['artifacts'] if m.get('name')==f'meeting-ai-source-qualification-{run_id}-{attempt}']
    require(len(found)==1 and found[0].get('expired') is False,'SOURCE_PROOF_MISSING')
    metadata=found[0]; linked=metadata.get('workflow_run',{})
    require(linked.get('id')==run_id and linked.get('head_sha')==head
        and linked.get('repository_id')==linked.get('head_repository_id')==source.REPOSITORY_ID, 'ARTIFACT_RUN_BINDING')
    raw=api.artifact_bytes(metadata)
    require('sha256:'+hashlib.sha256(raw).hexdigest()==metadata.get('digest'),'ARTIFACT_DIGEST')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist()==['source-qualification.json'],'ARTIFACT_FILES')
        item=archive.infolist()[0]
        require(0<item.file_size<=source.MAX_PROOF and not item.flag_bits&1,'ARTIFACT_EXPANSION')
        proof=gate.decode(archive.read(item).decode())
    require(proof.get('schema_version')==2 and proof.get('repository')==REPO and proof.get('repository_id')==source.REPOSITORY_ID
        and proof.get('run_id')==run_id and proof.get('run_attempt')==attempt and proof.get('status')=='passed'
        and proof.get('code') is None and proof.get('authorized_to_apply') is False
        and proof.get('producer_commit')==proof.get('head_sha')==head
        and proof.get('trusted_commit')==request['source_pr']['base_sha'] and proof.get('tree_sha')==request['source_pr']['tree_sha']
        and proof.get('source_pr')=={'number':request['source_pr']['number'],'bot_id':29139614}
        and proof.get('code_sha256')==config['trusted_code'], 'SOURCE_PROOF_IDENTITY')
    require(source.stamp(proof['observed_at'])<=now, 'FUTURE_SOURCE_PROOF')
    return proof, run, {'id':metadata['id'],'digest':metadata['digest']}


def observe(request,config,env,event,api,root,now,*,clock=lambda:datetime.now(timezone.utc),baseline_lookup=source.published_baseline,merged_sha=None):
    validate_request(request,config,env,event,now)
    identity=api.get(PREFIX); require(identity.get('id')==source.REPOSITORY_ID and identity.get('full_name')==REPO,'REPOSITORY_IDENTITY')
    baseline=baseline_lookup(api,root,include_manifest=True)
    require(baseline['manifest']==request['base_manifest'],'PUBLISHED_BASELINE_DRIFT')
    pr=request['source_pr']
    expected_main=merged_sha or pr['base_sha']
    def expected_pr(value):
        return (value.get('state')=='closed' and value.get('merged') is True and value.get('merge_commit_sha')==merged_sha) if merged_sha else (value.get('state')=='open' and value.get('merged') is False)
    if merged_sha:
        require(isinstance(merged_sha,str) and gate.SHA.fullmatch(merged_sha), 'MERGE_IDENTITY_UNKNOWN')
        commit=api.get(PREFIX+'/commits/'+merged_sha)
        require(commit.get('sha')==merged_sha and [p.get('sha') for p in commit.get('parents',[])]==[pr['base_sha'],pr['head_sha']]
            and commit.get('commit',{}).get('tree',{}).get('sha')==pr['tree_sha'], 'MERGE_IDENTITY_UNKNOWN')
    live=api.get(PREFIX+'/pulls/'+str(pr['number']))
    require(expected_pr(live) and live.get('draft') is False
        and live.get('user',{}).get('id')==29139614 and live['user'].get('login')=='renovate[bot]'
        and live['user'].get('type')=='Bot' and live.get('base',{}).get('ref')=='main','RENOVATE_PR_REQUIRED')
    for side,key in (('base','base_sha'),('head','head_sha')):
        require(live.get(side,{}).get('sha')==pr[key] and live[side].get('repo',{}).get('full_name')==REPO
            and live[side]['repo'].get('id')==source.REPOSITORY_ID,'PR_SOURCE_DRIFT')
    snapshots={sha:tree(api,sha) for sha in {baseline['commit'],pr['base_sha'],pr['head_sha']}}
    require(snapshots[pr['head_sha']]['tree']==pr['tree_sha'],'HEAD_TREE_DRIFT')
    for path,expected in config['trusted_code'].items():
        raw=(root/path).read_bytes(); entry={'mode':'100644','oid':gate.git_oid('blob',raw)}
        require(hashlib.sha256(raw).hexdigest()==expected and all(s['files'].get(path)==entry for s in snapshots.values()), 'TRUSTED_CODE_DRIFT')
    proof,run,artifact=ci_proof(api,request,config,now)
    require(request['source_proof']=={'run_id':run['id'],'run_attempt':run['run_attempt'],
        'artifact_id':artifact['id'],'digest':artifact['digest']}, 'SOURCE_PROOF_REFERENCE_DRIFT')
    source.workflow_identity(api,proof['workflow_commit'],pr['base_sha'],pr['head_sha'],pr['tree_sha'])
    require(hashlib.sha256(source.blob(api,'.github/workflows/ci.yml',proof['workflow_commit'])).hexdigest()==config['trusted_code']['.github/workflows/ci.yml'],'WORKFLOW_CODE_DRIFT')
    require(proof['baseline']=={k:v for k,v in baseline.items() if k!='manifest'},'SOURCE_BASELINE_DRIFT')
    delta=proof['classification']
    policy_change(request,delta.get('change'),clock())
    require(delta.get('delta_sha256')==gate.sha256({k:v for k,v in delta.items() if k!='delta_sha256'})
        and delta.get('base_commit')==baseline['commit'] and delta.get('base_tree')==snapshots[baseline['commit']]['tree']
        and delta.get('candidate_commit')==pr['head_sha'] and delta.get('candidate_tree')==pr['tree_sha']
        and delta.get('authorized_to_apply') is False,'SOURCE_DELTA_DRIFT')
    changed=sorted(p for p in snapshots[baseline['commit']]['files'].keys()|snapshots[pr['head_sha']]['files'].keys()
        if snapshots[baseline['commit']]['files'].get(p)!=snapshots[pr['head_sha']]['files'].get(p))
    require(changed==delta['changed_files'] and changed and set(changed)<=gate.ALLOWED,'CUMULATIVE_FEATURE_OR_CONTROL_DELTA')
    observed=clock(); registry,registry_rows=source.metadata_review(delta,proof['metadata_inputs'],api,observed,14)
    require(api.read('https://registry.npmjs.org/-/npm/v1/security/advisories/bulk',payload=proof['advisory_packages'])=={},'OFFICIAL_ADVISORY_REVIEW')
    latest=api.get(PREFIX+'/pulls/'+str(pr['number'])); latest_run=api.get(PREFIX+'/actions/runs/'+str(run['id']))
    latest_runs=api.get(PREFIX+'/actions/workflows/ci.yml/runs?head_sha='+pr['head_sha']+'&per_page=30')
    require(latest_runs.get('total_count')==len(latest_runs.get('workflow_runs',[])) and
        0<latest_runs['total_count']<30 and max(r['id'] for r in latest_runs['workflow_runs'])==run['id'], 'NEW_CI_RUN')
    require(api.get(PREFIX+'/commits/main').get('sha')==expected_main and expected_pr(latest)
        and latest.get('head',{}).get('sha')==pr['head_sha'] and latest.get('base',{}).get('sha')==pr['base_sha']
        and latest_run.get('run_attempt')==run['run_attempt'] and latest_run.get('status')=='completed'
        and latest_run.get('conclusion')=='success'
        and latest_run.get('head_sha')==pr['head_sha'],'FINAL_REF_OR_CI_DRIFT')
    require(baseline_lookup(api,root,include_manifest=True)==baseline,'FINAL_PUBLISHED_BASELINE_DRIFT')
    finished=clock(); validate_request(request,config,env,event,finished);policy_change(request,delta['change'],finished)
    require(source.stamp(proof['observed_at'])<=finished and source.stamp(run['updated_at'])<=finished, 'FUTURE_SOURCE_PROOF')
    fresh(registry['observed_at'],finished,timedelta(minutes=5))
    require(timedelta(0)<=finished-now<=timedelta(minutes=3),'RECEIVER_DEADLINE')
    return {'schema_version':1,'service':'meeting-ai','request_id':request['request_id'],'status':'source_ready',
        'source_pr':pr,'baseline':request['baseline'],'policy_sha256':request['policy_sha256'],
        'infra_commit':request['infra_commit'],'config_sha256':request['config_sha256'],'source_proof':request['source_proof'],
        'source_artifact':artifact,'evidence_run_id':run['id'],'evidence_run_attempt':run['run_attempt'],
        'delta_sha256':delta['delta_sha256'],'change':delta['change'],'scope':'dependency_updates','observed_at':finished.isoformat(),'registry':registry,'registry_rows':registry_rows,
        'merge_authorized':False,'deployment_authorized':False,
        'blocker':'MONTHLY_HOST_EXECUTOR_AND_RELEASE_HANDOFF_NOT_QUALIFIED'}


def main():
    names=('GH_TOKEN','GITHUB_REPOSITORY','GITHUB_REF','GITHUB_EVENT_NAME','GITHUB_RUN_ATTEMPT','GITHUB_RUN_ID',
        'GITHUB_SHA','GITHUB_ACTOR','GITHUB_ACTOR_ID','GITHUB_TRIGGERING_ACTOR','GITHUB_EVENT_PATH','PATH')
    selected={k:os.environ[k] for k in names if k in os.environ};os.environ.clear();os.environ.update(selected)
    os.environ.update(HOME='/nonexistent',LANG='C',GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null')
    require(len(sys.argv)==3,'REQUEST_AND_OUTPUT_REQUIRED')
    try:
        require(Path(sys.argv[1]).stat().st_size<=16384,'REQUEST_TOO_LARGE')
        request=gate.decode(Path(sys.argv[1]).read_text()); config=gate.decode((HERE.parent/'.github/maintenance.json').read_text())
        event=gate.decode(Path(os.environ['GITHUB_EVENT_PATH']).read_text()); now=datetime.now(timezone.utc)
        validate_request(request,config,os.environ,event,now)  # Before token/network.
        result=observe(request,config,os.environ,event,API(os.environ.get('GH_TOKEN')),HERE.parent,now)
    except Exception as error:
        code=str(error) if isinstance(error,gate.Refusal) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,90}',str(error)) else 'RECEIVER_UNKNOWN'
        # No identity from an unvalidated payload becomes execution evidence.
        result={'schema_version':1,'service':'meeting-ai','status':'refused','code':code,
                'merge_authorized':False,'deployment_authorized':False}
        Path(sys.argv[2]).write_bytes(gate.canonical(result)+b'\n')
        raise gate.Refusal(code) from None
    Path(sys.argv[2]).write_bytes(gate.canonical(result)+b'\n')


if __name__=='__main__':
    try:main()
    except Exception as error:
        code=str(error) if isinstance(error,gate.Refusal) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,90}',str(error)) else 'RECEIVER_UNKNOWN'
        print(code,file=sys.stderr);raise SystemExit(2)
