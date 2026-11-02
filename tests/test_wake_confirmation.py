"""Offline regression: wake acknowledgement is not proof that a VM is running."""
import importlib.util
import sys
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
from unittest.mock import patch

sys.modules['cdp'] = SimpleNamespace(CDP=object, http_json=lambda *args, **kwargs: {})
engine_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'engine.py'
spec = importlib.util.spec_from_file_location('candidate_engine', engine_path)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


def response(body, status=200):
    return SimpleNamespace(status_code=status, json=lambda: body, cookies=[])


initial = response({'status': 'assigned', 'vm_id': 'vm-fixture', 'vm_state': 'SUSPENDED'})
wake_ack = response({'accepted': True})
for verified, want_state, want_ok in [
    (response({'status': 'assigned', 'vm_state': 'SUSPENDED'}), 'SUSPENDED', False),
    (response({'status': 'assigned', 'vm_state': 'RUNNING'}), 'RUNNING', True),
    (response({}, status=503), 'SUSPENDED', False),
]:
    with patch('requests.get', side_effect=[initial, verified]), patch('requests.post', return_value=wake_ack):
        result = engine.MuseEngine.renew_session_http({'hatch_sess': 'fixture'})
    assert result['wake_requested'] is True
    assert result['wake_ok'] is want_ok
    assert result['vm_state'] == want_state
print('PASS: suspended, running, and failed-verification wake cases')
