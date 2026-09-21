#!/usr/bin/env python3
"""Read-only CI source proof. No merge, build, release or deploy authority.

Git/metadata helpers reuse Pluggy's reviewed source proof; Meeting keeps release
version selection separate from package.json. Producer runs on its own runner.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('gate',HERE/'maintenance-source-gate.py')
gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
require=gate.require
REPO='leodotsinc/meeting-ai';REPOSITORY_ID=1124034527;PREFIX='/repos/'+REPO
CODE={'.github/workflows/ci.yml','.github/workflows/deploy.yml','scripts/maintenance-source-gate.py',
      'scripts/maintenance-source-proof.py','scripts/maintenance-semver.cjs',
      'scripts/maintenance-tools/package.json','scripts/maintenance-tools/package-lock.json',
      'scripts/maintenance-tools/Dockerfile','scripts/maintenance-guard.py',
      'scripts/select-release.py','scripts/publish-release.py','scripts/release_manifest.py'}
MAX_PROOF=128*1024

def exact(value,keys,code):require(isinstance(value,dict) and set(value)==set(keys),code)
def stamp(value):
    require(isinstance(value,str),'TIMESTAMP_REQUIRED')
    result=datetime.fromisoformat(value.replace('Z','+00:00'));require(result.tzinfo is not None,'TIMEZONE_REQUIRED');return result

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


class API:
    def __init__(self, token, opener=None):
        require(isinstance(token,str) and token and len(token)<=4096,'TOKEN_REQUIRED')
        self.token=token;self.calls=0;self.registry_bytes=0;self.deadline=time.monotonic()+180
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    def read(self,url,*,payload=None):
        self.calls+=1;require(self.calls<=100 and time.monotonic()<self.deadline,'API_BUDGET')
        github=url=='https://api.github.com'+PREFIX or url.startswith('https://api.github.com'+PREFIX+'/')
        advisory=url=='https://registry.npmjs.org/-/npm/v1/security/advisories/bulk'
        registry=url.startswith('https://registry.npmjs.org/')
        require(github or registry,'API_ORIGIN')
        require(payload is None or advisory,'READ_ONLY_API')
        limit=4*1024*1024 if github or advisory else min(16*1024*1024,64*1024*1024-self.registry_bytes)
        require(limit>0,'REGISTRY_TOTAL_LIMIT')
        headers={'Accept':'application/json'}
        if github:headers['Authorization']='Bearer '+self.token
        if payload is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(url,headers=headers,data=gate.canonical(payload) if payload is not None else None)
        with self.opener.open(req,timeout=15) as response:
            require(response.status==200,'API_HTTP');raw=response.read(limit+1)
        require(len(raw)<=limit,'API_SIZE')
        if registry:self.registry_bytes+=len(raw)
        return gate.decode(raw.decode())
    def get(self,path):
        require((path==PREFIX or path.startswith(PREFIX+'/')) and not any(c in path for c in ('..','#','\\','\n','\r')),'API_PATH')
        return self.read('https://api.github.com'+path)
    def package(self,name):
        require(gate.NAME.fullmatch(name),'PACKAGE_NAME')
        return self.read('https://registry.npmjs.org/'+urllib.parse.quote(name,safe=''))

def blob(api,path,commit):
    require(gate.SHA.fullmatch(commit) and path in CODE,'CODE_PATH')
    value=api.get(PREFIX+'/contents/'+path+'?ref='+commit)
    require(value.get('encoding')=='base64','CODE_ENCODING')
    raw=base64.b64decode(value['content'],validate=False)
    require(len(raw)<=1024*1024 and gate.git_oid('blob',raw)==value.get('sha'),'CODE_HASH')
    return raw


def workflow_identity(api,workflow,base,head,tree):
    require(isinstance(workflow,str) and gate.SHA.fullmatch(workflow),'WORKFLOW_COMMIT')
    if workflow==head:return
    commit=api.get(PREFIX+'/git/commits/'+workflow)
    require(commit.get('sha')==workflow and [p.get('sha') for p in commit.get('parents',[])]==[base,head] and
            commit.get('tree',{}).get('sha')==tree,'WORKFLOW_MERGE_IDENTITY')


def source_inputs(before,after,delta):
    old=gate.validate_snapshot(before)[1]['packages'];new=gate.validate_snapshot(after)[1]['packages']
    inputs={c['path']:{'before':old[c['path']],'after':new[c['path']]} for c in delta['changes']}
    packages={}
    for path,item in new.items():
        if path:packages.setdefault(gate.package_name(path),set()).add(item['version'])
    return inputs,{name:sorted(versions) for name,versions in sorted(packages.items())}


def metadata_review(delta,inputs,api,now,minimum_days):
    """Recheck official metadata only; the source graph was classified in CI."""
    changes=delta['changes'];require(0<len(changes)<=20 and set(inputs)=={c['path'] for c in changes},'METADATA_INPUTS')
    metadata={name:api.package(name) for name in sorted({c['name'] for c in changes})}
    publications=[];rows=[]
    for c in changes:
        exact(inputs[c['path']],{'before','after'},'METADATA_INPUT_SCHEMA')
        value=metadata[c['name']];require(value.get('name')==c['name'],'NPM_PACKAGE_MISMATCH')
        old,new=value['versions'][c['before']],value['versions'][c['after']]
        for meta,label,version in ((old,'before',c['before']),(new,'after',c['after'])):
            lock=inputs[c['path']][label]
            require(meta.get('name')==c['name'] and meta.get('version')==version and
                    lock['version']==version and meta['dist']['integrity']==lock['integrity'] and
                    meta['dist']['tarball']==lock['resolved'],'NPM_SRI_MISMATCH')
            # A consumed package does not install its upstream development dependencies.
            for group in ('dependencies','optionalDependencies','peerDependencies'):
                require(meta.get(group,{})==lock.get(group,{}),'NPM_LOCK_EDGE_DRIFT')
        require(new['dist']['integrity']==c['integrity'] and new['dist']['tarball']==c['resolved'],'NPM_SRI_MISMATCH')
        require(not new.get('deprecated'),'DEPRECATED_CANDIDATE')
        require(not any(k in new.get('scripts',{}) for k in ('preinstall','install','postinstall','prepare')),'INSTALL_SCRIPT_REQUIRES_REVIEW')
        for field in ('scripts','bin','engines','exports','os','cpu','libc','type','main','module','types','typings','man'):
            require(old.get(field)==new.get(field),'NPM_BEHAVIOR_METADATA_CHANGED')
        published=stamp(value['time'][c['after']]);require(now-published>=timedelta(days=minimum_days),'NPM_RELEASE_TOO_YOUNG_OR_FUTURE')
        publications.append(published)
        rows.append({'name':c['name'],'version':c['after'],'integrity':c['integrity'],
                     'published_at':published.isoformat(),'checked_at':now.isoformat(),
                     'source':'https://registry.npmjs.org/'+urllib.parse.quote(c['name'],safe=''),
                     'advisories_source':'https://registry.npmjs.org/-/npm/v1/security/advisories/bulk',
                     'affecting_advisories':[]})
    return {'observed_at':now.isoformat(),'latest_publication':max(publications).isoformat()},rows


def published_baseline(api, root):
    """Reuse Meeting's published-asset/tag verifier; never accept a PR baseline."""
    sys.path.insert(0,str(HERE))
    spec=importlib.util.spec_from_file_location('publisher',HERE/'publish-release.py')
    publisher=importlib.util.module_from_spec(spec);spec.loader.exec_module(publisher)
    rows=api.get(PREFIX+'/releases?per_page=30')
    require(isinstance(rows,list) and len(rows)<30,'RELEASE_LIST_UNKNOWN')
    stable=[r for r in rows if r.get('draft') is False and r.get('prerelease') is False and
            isinstance(r.get('tag_name'),str) and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',r['tag_name'])]
    require(stable and len({r['tag_name'] for r in stable})==len(stable),'VERIFIED_BASELINE_ABSENT')
    release=max(stable,key=lambda r:gate.version(r['tag_name'][1:]));tag=release['tag_name']
    ref=api.get(PREFIX+'/git/ref/tags/'+tag)
    revision=publisher.tag_commit(REPO,tag,ref)
    result=publisher.verify_published_asset(REPO,tag,revision,release)
    return {'commit':revision,'tag':tag,'published_manifest_sha256':result['manifest_sha256']}


def emit_source(api,root,env,event,now,*,baseline_lookup=published_baseline,clock=lambda:datetime.now(timezone.utc)):
    require(env.get('GITHUB_REPOSITORY')==REPO and env.get('GITHUB_EVENT_NAME')=='pull_request','SOURCE_EVENT')
    pr=api.get(PREFIX+'/pulls/'+str(event['number']))
    require(type(pr.get('number')) is int and pr['number']==event['number'] and
            pr.get('user',{}).get('id')==29139614 and pr['user'].get('login')=='renovate[bot]' and
            pr['user'].get('type')=='Bot' and pr.get('state')=='open' and pr.get('draft') is False and
            pr.get('merged') is False and pr['base']['ref']=='main' and
            pr['head']['repo']['full_name']==pr['base']['repo']['full_name']==REPO,'SOURCE_PR_IDENTITY')
    head,base=pr['head']['sha'],pr['base']['sha']
    require(head==event['pull_request']['head']['sha'] and base==event['pull_request']['base']['sha'] and
            api.get(PREFIX+'/commits/main')['sha']==base,'SOURCE_BASE_DRIFT')
    before,after=gate.collect(root,base),gate.collect(root,head)
    hashes={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in sorted(CODE)}
    for path in CODE:
        require(before['files'].get(path)==after['files'].get(path)==
                {'mode':'100644','oid':gate.git_oid('blob',(root/path).read_bytes())},'SOURCE_CODE_DRIFT')
    workflow=env['GITHUB_WORKFLOW_SHA'];workflow_identity(api,workflow,base,head,after['tree'])
    require(hashlib.sha256(blob(api,'.github/workflows/ci.yml',workflow)).hexdigest()==hashes['.github/workflows/ci.yml'],
            'WORKFLOW_CODE_DRIFT')
    identity=api.get(PREFIX)
    require(identity.get('id')==REPOSITORY_ID and identity.get('full_name')==REPO,'SOURCE_REPOSITORY_IDENTITY')
    proof={'schema_version':2,'repository':REPO,'repository_id':REPOSITORY_ID,
        'run_id':int(env['GITHUB_RUN_ID']),'run_attempt':int(env['GITHUB_RUN_ATTEMPT']),
        'producer_commit':head,'workflow_commit':workflow,'trusted_commit':base,'head_sha':head,
        'tree_sha':after['tree'],'source_pr':{'number':pr['number'],'bot_id':29139614},
        'observed_at':now.isoformat(),'code_sha256':hashes,'status':'refused','code':'SOURCE_UNKNOWN',
        'baseline':None,'classification':None,'metadata_inputs':None,'advisory_packages':None,'registry':None,
        'release_version_strategy':'existing_verified_tag_selector','authorized_to_apply':False}
    try:
        baseline=baseline_lookup(api,root);proof['baseline']=baseline
        # Cumulative A→H, not merely the latest bot diff B→H. Tooling/bootstrap
        # and unrelated features therefore remain explicit refusal reasons.
        deployed=gate.collect(root,baseline['commit'])
        delta=gate.classify_source(deployed,after);inputs,packages=source_inputs(deployed,after,delta)
        proof.update(classification=delta,metadata_inputs=inputs,advisory_packages=packages)
        registry,_=metadata_review(delta,inputs,api,now,14)
        require(api.read('https://registry.npmjs.org/-/npm/v1/security/advisories/bulk',payload=packages)=={},
                'OFFICIAL_ADVISORY_REVIEW')
        proof.update(registry=registry,status='passed',code=None)
    except (gate.Refusal,ValueError) as error:
        code=str(error);proof['code']=code if re.fullmatch('[A-Z][A-Z0-9_]{2,90}',code) else 'SOURCE_UNKNOWN'
    latest=api.get(PREFIX+'/pulls/'+str(pr['number']))
    require(latest['head']['sha']==head and latest['base']['sha']==base and latest.get('state')=='open' and
            api.get(PREFIX+'/commits/main')['sha']==base,'SOURCE_CHANGED_DURING_REVIEW')
    require(timedelta(0)<=clock()-now<=timedelta(minutes=3),'SOURCE_REVIEW_DEADLINE')
    require(len(gate.canonical(proof))<=MAX_PROOF,'SOURCE_PROOF_SIZE')
    return proof


def main():
    selected={k:os.environ[k] for k in ('GH_TOKEN','GITHUB_REPOSITORY','GITHUB_EVENT_NAME','GITHUB_SHA',
        'GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT','GITHUB_WORKFLOW_SHA','GITHUB_EVENT_PATH','PATH') if k in os.environ}
    os.environ.clear();os.environ.update(selected);os.environ.update(HOME='/nonexistent',LANG='C',
        GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL='/dev/null',NODE_OPTIONS='',NODE_PATH='')
    require(len(sys.argv)==2,'OUTPUT_PATH_REQUIRED')
    root=HERE.parent;event=gate.decode(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    value=emit_source(API(os.environ.get('GH_TOKEN')),root,os.environ,event,datetime.now(timezone.utc))
    Path(sys.argv[1]).write_bytes(gate.canonical(value)+b'\n')


if __name__=='__main__':
    try:main()
    except Exception:print('SOURCE_PROOF_UNKNOWN',file=sys.stderr);raise SystemExit(2)
