#!/usr/bin/env python3
"""Normalize existing Meeting release evidence in the trusted producer job."""
from collections import Counter
from datetime import timedelta
import hashlib
import importlib.util
from pathlib import Path
import re

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('meeting_pipeline',HERE/'maintenance-pipeline.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
r=p.r;require=p.require;gate=p.gate
SEVERITIES={'CRITICAL','HIGH','MEDIUM','LOW','UNKNOWN'}
FUNCTIONAL={'readiness_database','anonymous_auth_boundary','fixture_login','authenticated_version',
    'runtime_database_stats','drain_blocks_mutations','leases_released'}


def read(path):
    require(path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=8*1024*1024,'EVIDENCE_FILE_INVALID')
    raw=path.read_bytes();return gate.decode(raw.decode()),hashlib.sha256(raw).hexdigest()


def normalize(inputs,request,context,config,env,event,api,root,now,*,refresh=p.release_evidence,clock=p.now):
    manifest,_=read(inputs/'release.json');image,_=read(inputs/'release-image.json')
    require(image=={'image':manifest['image'],'image_id':image.get('image_id'),'source_commit':manifest['git_sha'],
        'version':manifest['release_version'],'run_id':int(env['GITHUB_RUN_ID']),'run_attempt':int(env['GITHUB_RUN_ATTEMPT'])}
        and isinstance(image['image_id'],str) and re.fullmatch(r'sha256:[0-9a-f]{64}',image['image_id']), 'PUBLISHED_IMAGE_BINDING')
    functional,functional_hash=read(inputs/'meeting-functional.json')
    require(functional.get('ok') is True and functional.get('image_id')==image['image_id']
        and functional.get('version')==manifest['release_version'] and functional.get('revision')==manifest['git_sha']
        and str(functional.get('build_id'))==env['GITHUB_RUN_ID'] and functional.get('model_calls')==0
        and functional.get('network_internal') is True and functional.get('ports_published') is False
        and functional.get('external_processing') is False and functional.get('migration_readonly_image') is True
        and set(functional.get('checks',[]))==FUNCTIONAL,'FUNCTIONAL_PROOF_INCOMPLETE')
    restore=functional.get('synthetic_restore',{})
    require(all(restore.get(k) is True for k in ('database_separate','authenticated_projects','existing_session_preserved','original_application_preserved'))
        and isinstance(restore.get('upload_sha256'),str) and r.HASH.fullmatch(restore['upload_sha256'])
        and restore['upload_sha256']==restore.get('restored_upload_sha256'),'FUNCTIONAL_RESTORE_PROOF_INCOMPLETE')
    r.fresh(functional['observed_at'],now,timedelta(hours=24))
    scans=[]
    for stage in ('runtime','builder','deps'):
        row,report_hash=read(inputs/'security'/stage/'security.json')
        require(row.get('schema_version')==1 and row.get('status')=='passed' and isinstance(row.get('image'),str)
            and re.fullmatch(r'sha256:[0-9a-f]{64}',row['image']) and type(row.get('packages')) is int and row['packages']>0,
            'SCAN_IDENTITY_OR_COVERAGE')
        if stage=='runtime':require(row['image']==image['image_id'],'SCAN_RUNTIME_DRIFT')
        counts=row.get('counts');findings=row.get('findings')
        require(isinstance(counts,dict) and set(counts)==SEVERITIES and all(type(v) is int and v>=0 for v in counts.values())
            and isinstance(findings,list) and len(findings)<=10000,'SCAN_COUNTS_INVALID')
        observed=Counter(f.get('Severity') if f.get('Severity') in SEVERITIES else 'UNKNOWN' for f in findings)
        require(all(observed[s]==counts[s] for s in SEVERITIES) and all(counts[s]==0 for s in ('CRITICAL','HIGH','UNKNOWN')),'SCAN_FINDINGS_BLOCKED')
        r.fresh(row['observed_at'],now,timedelta(hours=24));r.fresh(row['database_updated_at'],now,timedelta(hours=24))
        scans.append({'component':stage,'image':row['image'],'status':'passed','observed_at':row['observed_at'],
            'database_updated_at':row['database_updated_at'],'report_sha256':report_hash,'packages':row['packages'],'findings':counts})
    fresh=refresh(request,context,manifest,config,env,event,api,root,clock=clock)
    now=clock()
    require(fresh.get('change') in ('patch','minor') and fresh.get('scope')=='dependency_updates','SOURCE_SCOPE_REQUIRED')
    registry=fresh['registry'];require(isinstance(registry,list) and registry,'REGISTRY_PROOF_MISSING')
    for row in registry:
        r.fresh(row['checked_at'],now,timedelta(minutes=5))
        require(now-r.source.stamp(row['published_at'])>=timedelta(days=14) and row['affecting_advisories']==[], 'REGISTRY_NOT_QUALIFIED')
    return {'schema_version':1,'app':'meeting-ai','repository':r.REPO,'run_id':int(env['GITHUB_RUN_ID']),
        'run_attempt':int(env['GITHUB_RUN_ATTEMPT']),'producer_commit':context['producer_commit'],
        'source_commit':context['merged_sha'],'context_sha256':gate.sha256(context),'release_sha256':gate.sha256(manifest),
        'images':{'app':manifest['image']},'image_ids':{'app':image['image_id']},'source_proof':request['source_proof'],'delta_sha256':context['delta_sha256'],
        'change':fresh['change'],'scope':fresh['scope'],'code_sha256':config['trusted_code'],'observed_at':now.isoformat(),
        'registry':{'status':'passed','observed_at':min(row['checked_at'] for row in registry),
            'latest_publication':max(row['published_at'] for row in registry),
            'advisories_source':'https://registry.npmjs.org/-/npm/v1/security/advisories/bulk'},
        'functional':{'status':'passed','observed_at':functional['observed_at'],'report_sha256':functional_hash},'scans':scans}
