"""Tasks know which account ran them, including attempts that ended in failure."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("task_account_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)

    first = app.store.add_account({"hatch_sess": "one", "hatch_vml": "one",
                                   "hatch_native_auth_device": "one"}, "one")
    second = app.store.add_account({"hatch_sess": "two", "hatch_vml": "two",
                                    "hatch_native_auth_device": "two"}, "two")
    seen = []

    def boom(cookies, prompt, **kwargs):
        # Auth failure marks the account unhealthy, so the retry lands on the other one.
        raise app.MuseAuthError("会话失效")

    task = app.store.create_task("image", "fixture")
    with patch.object(app, "_renew_and_persist",
                      side_effect=lambda ident, **_: app.store.get_account(ident)), \
         patch.object(app.engine, "start"), \
         patch.object(app.engine, "generate", side_effect=boom), \
         patch.object(app.engine, "reset_thread"), \
         patch.object(app.engine, "close_current_page"):
        try:
            app._run_generation("image", "image", 20, account_id=first["id"],
                                on_account=lambda a: (seen.append(a["id"]),
                                                      app.store.update_task(task["id"], account=a["id"])))
        except app.MuseAuthError:
            pass
        else:
            raise AssertionError("generation error was not propagated")

    assert seen == [first["id"], second["id"]], seen
    assert app.store.get_task(task["id"])["account"] == second["id"], app.store.get_task(task["id"])
    assert [t.get("account") for t in app.store.list_tasks(10)] == [second["id"]]

print("PASS: task records keep the account id for the first and the rotated attempt")
