"""A video delivery uncertainty must not silently cause a second generation."""
import os,sys,tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import MuseEngine,MuseGenerationError,MuseSubmissionUncertainError
with tempfile.TemporaryDirectory() as home:
 e=MuseEngine(SimpleNamespace(profile_dir=home,media_dir=home))
 e.ensure_page=lambda *a,**k:None
 e.reset_thread=lambda **k:None
 e._scroll_bottom=lambda:None
 e._clear_attachments=lambda:None
 e.attachments=lambda:[]
 e._agent_count=lambda:0
 e._submission_state=lambda prompt:{}
 e._send_confirmable=lambda prompt:'clicked'
 e._confirm_submission=lambda prompt,before,timeout:{'path':'/thread/abcdef12-0000-0000-0000-000000000000'}
 e._wait_attachment=lambda *a,**k:(_ for _ in ()).throw(MuseGenerationError('fixture disconnect'))
 e._debug_dump=lambda *a:None
 events=[]
 try:e.generate({'hatch_sess':'fixture'},'test',expect='video',timeout=20,on_submission=events.append)
 except MuseSubmissionUncertainError as exc:assert '不自动重发' in str(exc)
 else:raise AssertionError('delivered request was not marked uncertain')
 assert len(events)==1 and events[0]['provider_thread'].startswith('/thread/') and events[0]['prompt_sha256']
 print('PASS: confirmed video delivery is persisted and failures do not trigger automatic resend')
