"""Import the application using only files declared by Docker COPY, without Docker."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

with tempfile.TemporaryDirectory(prefix='muse-docker-copy-') as directory:
    image = Path(directory)
    for line in (ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines():
        if not line.startswith('COPY '):
            continue
        arguments = shlex.split(line)[1:]
        destination = image / arguments[-1].lstrip('/')
        for filename in arguments[:-1]:
            source = ROOT / filename
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination / source.name)
    environment = os.environ.copy()
    environment.update(MUSE2API_HOME=str(image), MUSE2API_PROFILE_ROOT=str(image / 'profile'),
                       MUSE2API_KEY='test-only', PYTHONPATH=str(image), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c',
        'import app; assert callable(app.native_logins.start); app.native_logins.shutdown()'],
        cwd=image, env=environment, capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)
print('PASS: Docker COPY files include all modules required to import the app; no image build or network')
