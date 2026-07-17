#!/usr/bin/env python3
"""Parallel cache-replay fallback for the six mechanistic-only cases.
Read-only; calls the deployed public TargetVal validation API and saves full JSON.
"""
from __future__ import annotations
import concurrent.futures, json, os, time, urllib.error, urllib.request
from pathlib import Path

BASE=os.environ.get('TARGETVAL_BASE_URL','https://targetval-api.onrender.com').rstrip('/')
OUT=Path(os.environ.get('AUDIT_OUT','audit_results/mechanistic_replay')); OUT.mkdir(parents=True,exist_ok=True)
CASES=[
 ('01_gdf15_obesity','GDF15','obesity'),
 ('02_gdf15_cancer_cachexia','GDF15','cancer cachexia'),
 ('03_fgf21_mash','FGF21','metabolic dysfunction-associated steatohepatitis'),
 ('04_il10_ibd','IL10','inflammatory bowel disease'),
 ('05_hhla2_nsclc','HHLA2','non-small cell lung cancer'),
 ('07_col1a1_oi','COL1A1','osteogenesis imperfecta'),
]

def one(case):
 cid,g,d=case; payload=json.dumps({'gene_symbol':g,'disease_name':d,'mode':'SETTLE'}).encode(); attempts=[]
 for n in range(1,5):
  st=time.monotonic(); status=None; body=''; err=''
  req=urllib.request.Request(BASE+'/api/v1/validate',data=payload,method='POST',headers={'Content-Type':'application/json','Accept':'application/json','User-Agent':'TargetVal-mech-cache-replay/2026-07-17'})
  try:
   with urllib.request.urlopen(req,timeout=360) as r: status=r.status; body=r.read().decode('utf-8','replace')
  except urllib.error.HTTPError as e: status=e.code; body=e.read().decode('utf-8','replace'); err=str(e)
  except Exception as e: err=f'{type(e).__name__}: {e}'
  attempts.append({'attempt':n,'status':status,'elapsed_s':round(time.monotonic()-st,3),'error':err,'body_prefix':body[:500]})
  if status==200:
   try: return {'id':cid,'gene':g,'disease':d,'ok':True,'attempts':attempts,'response':json.loads(body)}
   except Exception as e: err=f'json: {e}'
  if n<4: time.sleep(20*n)
 return {'id':cid,'gene':g,'disease':d,'ok':False,'attempts':attempts,'error':err}

with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
 rows=list(ex.map(one,CASES))
for r in rows:
 (OUT/(r['id']+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
(OUT/'summary.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps([{'id':r['id'],'ok':r['ok'],'attempts':r['attempts']} for r in rows],indent=2))
