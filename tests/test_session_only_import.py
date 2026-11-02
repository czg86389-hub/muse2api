"""Importer accepts a session-only cookie; live auth still decides validity."""
import os,sys,tempfile,asyncio,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
home=tempfile.TemporaryDirectory()
os.environ.update(MUSE2API_HOME=home.name,MUSE2API_PROFILE_ROOT=home.name,MUSE2API_KEY='test-only')
import app,requests
req=app.AccountRequest(cookies={'hatch_sess':'fixture'})
result=app.add_account(req)
assert result['essential_missing']==[] and result['warning']==''
aid=result['added'][0]['id']
assert app.store.get_account(aid)['ok'] is None
assert app.store.list_accounts()[0]['essential_ok']
for code,body,want in [(200,{'status':'assigned'},True),(401,{},False),(200,{'status':'unavailable'},False)]:
 reply=SimpleNamespace(status_code=code,json=lambda:body,cookies=[])
 with patch('requests.get',return_value=reply):
  r=app._probe_account_sync(aid)
 assert r['ok'] is want
 if code==401:assert app.store.get_account(aid)['ok'] is False
print('PASS: session-only import, unknown initial health, assigned accepted, 401 rejected, unavailable not accepted')
home.cleanup()
