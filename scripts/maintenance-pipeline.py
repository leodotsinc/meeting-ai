#!/usr/bin/env python3
"""Meeting pipeline handoff. Source admission and native deployment stay authoritative."""
from datetime import datetime, timedelta, timezone
import importlib.util
import os
from pathlib import Path
import re
import sys
import urllib.request

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('meeting_receiver', HERE / 'maintenance-receiver.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
gate, source, require = r.gate, r.source, r.require


def now(): return datetime.now(timezone.utc)


class API(r.API):
    def merge_once(self, number, head):
        require(type(number) is int and 0 < number < 1000000 and gate.SHA.fullmatch(head), 'MERGE_INPUT')
        request = urllib.request.Request('https://api.github.com' + r.PREFIX + f'/pulls/{number}/merge',
            data=gate.canonical({'sha': head, 'merge_method': 'merge'}), method='PUT',
            headers={'Accept':'application/vnd.github+json','Content-Type':'application/json',
                     'Authorization':'Bearer '+self.token})
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(16385)
                require(response.status == 200 and len(raw) <= 16384, 'MERGE_RESULT_UNKNOWN')
                return gate.decode(raw.decode())
        except Exception:
            raise gate.Refusal('MERGE_RESULT_UNKNOWN_RECONCILE') from None


def merged_identity(commit, main, request):
    pr = request['source_pr']; merged = commit.get('sha')
    require(isinstance(merged,str) and gate.SHA.fullmatch(merged) and merged not in (pr['base_sha'],pr['head_sha']) and
        main.get('sha') == merged and [p.get('sha') for p in commit.get('parents',[])] == [pr['base_sha'],pr['head_sha']] and
        commit.get('commit',{}).get('tree',{}).get('sha') == pr['tree_sha'], 'MERGE_IDENTITY_UNKNOWN')
    return merged


def save(path, value, *, exclusive=False):
    """Durable attempt evidence; ambiguous writes are never retried as a merge."""
    path = Path(path)
    target = path if exclusive else path.with_name(path.name + '.pending')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(gate.canonical(value)); stream.flush(); os.fsync(stream.fileno())
    if not exclusive: os.replace(target, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def require_host_transport(config, request):
    # Root register authenticates this installed capability before dispatch;
    # the host independently checks it again under its existing native locks.
    require(isinstance(config.get('host_contract_sha256'),str) and
        r.HASH.fullmatch(config['host_contract_sha256']) and
        request.get('host_contract_sha256')==config['host_contract_sha256'], 'HOST_CONTRACT_NOT_REGISTERED')


def reservation(api, request, env):
    name = 'meeting-ai-maintenance-attempt-' + request['request_id']
    rows = api.get(r.PREFIX + '/actions/artifacts?name=' + name + '&per_page=100')
    items = rows.get('artifacts')
    require(isinstance(items,list) and rows.get('total_count')==len(items)==1 and
        items[0].get('name')==name and items[0].get('expired') is False and
        items[0].get('workflow_run',{}).get('id')==int(env['GITHUB_RUN_ID']) and
        items[0]['workflow_run'].get('head_sha')==request['source_pr']['base_sha'],
        'MERGE_ATTEMPT_REQUIRES_RECONCILIATION')


def context(request, admission, merged, env):
    require(admission['request_id']==request['request_id'] and admission['status']=='source_ready', 'SOURCE_ADMISSION_REQUIRED')
    return {'schema_version':1,'app':'meeting-ai','mode':'monthly','request_id':request['request_id'],
        'infra_commit':request['infra_commit'],'config_sha256':request['config_sha256'],
        'host_contract_sha256':request['host_contract_sha256'],
        'policy_sha256':request['policy_sha256'],'source_proof':request['source_proof'],
        'baseline_receipt_sha256':request['baseline']['receipt_sha256'],'source_pr':request['source_pr']['number'],
        'head_sha':request['source_pr']['head_sha'],'merged_sha':merged,'tree_sha':request['source_pr']['tree_sha'],
        'delta_sha256':admission['delta_sha256'],'window':request['window'],'expires_at':request['expires_at'],
        'base_manifest':request['base_manifest'],'producer_commit':request['source_pr']['base_sha'],
        'evidence_run_id':admission['evidence_run_id'],'release_run_id':int(env['GITHUB_RUN_ID'])}


def preflight(request, supplied, config, env, event, api, root, *, clock=now,
              admit=r.observe, host_ready=None):
    r.validate_request(request,config,env,event,clock())
    (host_ready or (lambda:require_host_transport(config,request)))()
    require(isinstance(supplied,dict), 'CONTEXT_REQUIRED')
    merged=supplied.get('merged_sha')
    admission=admit(request,config,env,event,api,root,clock(),clock=clock,merged_sha=merged)
    require(supplied==context(request,admission,merged,env), 'CONTEXT_DRIFT')
    require(source.stamp(request['expires_at'])-clock()>=timedelta(seconds=900), 'INSUFFICIENT_RELEASE_WINDOW')
    return supplied


def merge(request, config, env, event, api, root, journal, *, clock=now, admit=r.observe, host_ready=None):
    r.validate_request(request,config,env,event,clock())
    (host_ready or (lambda:require_host_transport(config,request)))()
    reservation(api,request,env)
    require(not os.path.lexists(journal), 'MERGE_ATTEMPT_REQUIRES_RECONCILIATION')
    admission = admit(request, config, env, event, api, root, clock(), clock=clock)
    require(admission['status'] == 'source_ready' and admission['request_id'] == request['request_id'], 'SOURCE_ADMISSION_REQUIRED')
    pr = request['source_pr']
    live = api.get(r.PREFIX + '/pulls/' + str(pr['number']))
    require(live.get('state') == 'open' and live.get('head',{}).get('sha') == pr['head_sha'] and
        live.get('base',{}).get('sha') == pr['base_sha'] and
        api.get(r.PREFIX+'/commits/main').get('sha') == pr['base_sha'], 'SOURCE_CHANGED_BEFORE_MERGE')
    at = clock(); r.validate_request(request, config, env, event, at)
    require(source.stamp(request['expires_at']) - at >= timedelta(seconds=900), 'INSUFFICIENT_RELEASE_WINDOW')
    record = {'schema_version':1,'request_id':request['request_id'],'state':'merge_pending_reconciliation',
              'head_sha':pr['head_sha'],'source_artifact':admission['source_artifact']}
    save(journal, record, exclusive=True)
    try:
        answer = api.merge_once(pr['number'], pr['head_sha'])
        require(answer.get('merged') is True and gate.SHA.fullmatch(answer.get('sha','')), 'MERGE_RESULT_UNKNOWN')
        merged = merged_identity(api.get(r.PREFIX+'/commits/'+answer['sha']), api.get(r.PREFIX+'/commits/main'), request)
    except Exception:
        # Keep the already durable pending record. Never issue a second PUT.
        raise gate.Refusal('MERGE_RESULT_UNKNOWN_RECONCILE') from None
    record.update(state='merged_pending_build', merged_sha=merged); save(journal, record)
    r.validate_request(request, config, env, event, clock())
    return admission, merged


def release_evidence(request, supplied, manifest, config, env, event, api, root, *, clock=now):
    admissions=[]
    def admit(*args,**kwargs):
        value=r.observe(*args,**kwargs);admissions.append(value);return value
    checked=preflight(request,supplied,config,env,event,api,root,clock=clock,admit=admit)
    sys.path.insert(0,str(HERE));from release_manifest import validate
    manifest=validate(manifest)
    require(manifest['service']=='meeting-ai' and manifest['git_sha']==checked['merged_sha'] and
        manifest['source_repository']=='https://github.com/'+r.REPO and
        str(manifest['build']['id'])==env['GITHUB_RUN_ID'] and manifest['build']['attempt']==int(env['GITHUB_RUN_ATTEMPT']) and
        re.fullmatch(r'ghcr.io/leodotsinc/meeting-ai@sha256:[0-9a-f]{64}',manifest['image']), 'RELEASE_MANIFEST_DRIFT')
    return {'schema_version':1,'service':'meeting-ai','repository':r.REPO,
        'run_id':int(env['GITHUB_RUN_ID']),'run_attempt':int(env['GITHUB_RUN_ATTEMPT']),
        'producer_commit':checked['producer_commit'],'source_commit':checked['merged_sha'],
        'context_sha256':gate.sha256(checked),'source_proof':request['source_proof'],
        'delta_sha256':checked['delta_sha256'],'release_manifest_sha256':gate.sha256(manifest),
        'image':manifest['image'],'change':admissions[0]['change'],'scope':admissions[0]['scope'],'registry':admissions[0]['registry_rows'],'observed_at':clock().isoformat(),
        'deployment_authorized':False}


def main():
    names=('GH_TOKEN','GITHUB_REPOSITORY','GITHUB_REF','GITHUB_EVENT_NAME','GITHUB_RUN_ATTEMPT','GITHUB_RUN_ID',
        'GITHUB_SHA','GITHUB_ACTOR','GITHUB_ACTOR_ID','GITHUB_TRIGGERING_ACTOR','GITHUB_EVENT_PATH','PATH',
        'GITHUB_OUTPUT','MAINTENANCE_REQUEST','MAINTENANCE_CONTEXT')
    selected={k:os.environ[k] for k in names if k in os.environ};os.environ.clear();os.environ.update(selected)
    os.environ.update(HOME='/nonexistent',LANG='C',GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null')
    require(len(sys.argv)==2 and sys.argv[1] in ('admit','merge','preflight','refresh'), 'PIPELINE_MODE_INVALID')
    mode=sys.argv[1];raw=os.environ.get('MAINTENANCE_REQUEST','')
    require(0<len(raw.encode())<=16384, 'PIPELINE_INPUT_SIZE');request=gate.decode(raw)
    config=gate.decode((HERE.parent/'.github/maintenance.json').read_text())
    event=gate.decode(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    r.validate_request(request,config,os.environ,event,now());require_host_transport(config,request)
    api=API(os.environ.get('GH_TOKEN')); outputs={}
    if mode=='admit':
        result=r.observe(request,config,os.environ,event,api,HERE.parent,now())
        save('maintenance-admission.json',result,exclusive=True);outputs['request_id']=request['request_id']
    elif mode=='merge':
        admission,merged=merge(request,config,os.environ,event,api,HERE.parent,'maintenance-merge.json')
        result=context(request,admission,merged,os.environ)
        save('maintenance-context.json',result,exclusive=True)
        outputs={'sha':merged,'context':gate.canonical(result).decode()}
    else:
        raw=os.environ.get('MAINTENANCE_CONTEXT','');require(0<len(raw.encode())<=32768,'PIPELINE_INPUT_SIZE')
        supplied=gate.decode(raw)
        if mode=='preflight':
            result=preflight(request,supplied,config,os.environ,event,api,HERE.parent);outputs['sha']=result['merged_sha']
        else:
            spec=importlib.util.spec_from_file_location('release_proof',HERE/'maintenance-release-proof.py')
            normalizer=importlib.util.module_from_spec(spec);spec.loader.exec_module(normalizer)
            result=normalizer.normalize(Path('maintenance-inputs'),request,supplied,config,os.environ,event,api,HERE.parent,now())
            save('maintenance-release.json',result,exclusive=True)
    if outputs:
        with open(os.environ['GITHUB_OUTPUT'],'a') as output:
            for key,value in outputs.items():output.write(key+'='+value+'\n')


if __name__=='__main__':
    try:main()
    except Exception as error:
        code=str(error) if isinstance(error,gate.Refusal) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,90}',str(error)) else 'PIPELINE_HANDOFF_UNKNOWN'
        print(code,file=sys.stderr);raise SystemExit(2)
