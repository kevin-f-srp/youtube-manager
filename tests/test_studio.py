"""Run with python -m unittest discover -s tests -p test_studio.py."""

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from src.studio.app import create_app
from src.studio.store import Conflict, apply_proposal, validate
from src.studio.harness import patch_for


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw = self.root / "trip"
        self.raw.mkdir()
        Image.new("RGB", (80, 120), "green").save(self.raw / "photo.jpg")
        self.app = create_app(self.root / "data")
        self.client = self.app.test_client()
        self.token = self.client.get("/api/config").json["token"]
        self.headers = {"X-Studio-Token": self.token}
        self.store = self.app.extensions["studio_store"]
        self.jobs = self.app.extensions["studio_jobs"]
        self.pid = self.store.register(self.raw)
        self.base = "/api/projects/" + self.pid

    def tearDown(self):
        self.jobs.close()
        self.tmp.cleanup()

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def draft(self):
        current = self.store.load(self.pid)
        edl = copy.deepcopy(current["edl"])
        edl["shots"] = [
            {"id": "s1", "clip": "photo.jpg", "in": 0, "out": 3, "audio": "mute"}
        ]
        return current, edl

    def test_review_anchor_source_and_invalid_time(self):
        from src.studio.context import resolve_context
        current, edl = self.draft()
        self.store.save(self.pid, edl, current['revision'])
        asset = current['assets'][0]
        context, path = resolve_context(self.store, self.jobs, self.pid,
            {'kind':'source', 'asset_id':asset['id'], 'time':0})
        self.assertEqual(context['shot_ids'], ['s1'])
        self.assertEqual(path, (self.raw / 'photo.jpg').resolve())
        for t in [-1, float('nan'), '2', True]:
            with self.assertRaises(ValueError):
                resolve_context(self.store, self.jobs, self.pid,
                    {'kind':'source', 'asset_id':asset['id'], 'time':t})
        with self.assertRaises(ValueError):
            resolve_context(self.store, self.jobs, self.pid,
                {'kind':'source', 'asset_id':'unknown', 'time':0})

    def test_explanation_has_no_applicable_changes(self):
        from src.studio.store import proposal_changes, write_json
        current, edl = self.draft()
        rev = self.store.save(self.pid, edl, current['revision'])
        proposal = {'summary':'仅解释，不修改', 'shots':edl['shots']}
        self.assertEqual(proposal_changes(edl, proposal), [])
        changed = copy.deepcopy(proposal)
        changed['shots'][0]['out'] = 2
        changes = proposal_changes(edl, changed)
        self.assertEqual(changes, [{'shot_id':'s1','field':'out','before':3,'after':2}])
        jid = 'e' * 32
        folder = self.store.directory(self.pid) / 'jobs' / jid
        write_json(folder / 'job.json', {'id':jid,'state':'completed','kind':'agent','created':0})
        write_json(folder / 'input-edl.json', edl)
        write_json(folder / 'result.json', {'proposal':proposal,'base_revision':rev})
        response = self.post(self.base + '/jobs/' + jid + '/apply', {})
        self.assertEqual(response.status_code, 400)
        self.assertIn('没有实际修改', response.json['error'])
        self.assertEqual(self.store.load(self.pid)['revision'], rev)
        self.assertEqual(self.client.get(self.base + '/jobs').json[0]['result']['changes'], [])

    def test_model_auth_error_is_actionable_and_does_not_echo_key(self):
        from src.studio.harness import failure_message
        from types import SimpleNamespace
        result = SimpleNamespace(finish_reason='error', events=[{'type':'turn/end',
            'data':{'reason':{'kind':'error','error':{'message':
                'OpenAI API error (401): Incorrect API key provided: private-test-secret'}}}}])
        message = failure_message(result)
        self.assertIn('401', message)
        self.assertIn('兼容服务地址', message)
        self.assertNotIn('private-test-secret', message)

    def test_all_384_assets_fit_image_budget_without_omissions(self):
        from src.studio.worker import make_sheets
        folder = self.root / 'sheets'
        (folder / 'thumbs').mkdir(parents=True)
        Image.new('RGB', (350, 280), 'red').save(folder / 'thumbs' / 'source.jpg')
        assets = [{'id':f'a{i:04}', 'thumbnail':'source.jpg'} for i in range(384)]
        with patch('src.studio.worker.ImageDraw.Draw') as draw:
            images = make_sheets(folder, assets, folder)
            labels = [call.args[1] for call in draw.return_value.text.call_args_list]
        self.assertEqual(labels, [a['id'] for a in assets])
        self.assertEqual(len(images), 16)
        self.assertLessEqual(len(images) + 1, 20)
        with Image.open(images[-1][1]) as image:
            self.assertEqual(image.size, (1440,1920))

    def test_agent_receives_located_frame(self):
        from src.studio.worker import agent, scan
        from src.studio.store import write_json
        current, edl = self.draft()
        self.store.save(self.pid, edl, current['revision'])
        folder = self.store.directory(self.pid)
        job = folder / 'jobs' / ('b' * 32)
        job.mkdir(parents=True)
        scan(folder, job)
        write_json(job / 'input-edl.json', edl)
        req = {'message':'保留这个画面', 'provider':'openai', 'model':'mock',
               'context':{'kind':'source', 'asset_id':current['assets'][0]['id'], 'time':0}}
        with patch('src.studio.harness.propose', return_value={'summary':'保留', 'shots':edl['shots']}) as model:
            agent(folder, job, req)
            args = model.call_args.args
            self.assertIn('review_context', args[3])
            self.assertIn('s1', args[3])
            self.assertEqual(args[4][0][1].name, 'context-frame.jpg')
            with Image.open(args[4][0][1]) as image:
                self.assertEqual(image.size, (80,120))

    def test_cut_anchor_uses_render_snapshot_with_skip_and_speed(self):
        from src.studio.context import resolve_context, segments
        from src.studio.store import write_json
        shot = {'id':'old-shot', 'clip':'movie', 'in':10, 'out':20,
                'skip':[[12,14]], 'speed':2}
        self.assertEqual(segments(shot), [(10,12),(14,20)])
        jid = 'a' * 32
        folder = self.store.directory(self.pid) / 'jobs' / jid
        folder.mkdir(parents=True)
        output = folder / 'preview.mp4'
        output.touch()
        write_json(folder / 'job.json', {'state':'completed'})
        write_json(folder / 'result.json', {'output':str(output), 'duration':4})
        write_json(folder / 'input-edl.json', {'shots':[shot]})
        context, _ = resolve_context(self.store, self.jobs, self.pid,
            {'kind':'cut', 'job_id':jid, 'time':2})
        self.assertEqual(context['shot_ids'], ['old-shot'])
        self.assertEqual(context['source_time'], 16)
        with self.assertRaises(ValueError):
            resolve_context(self.store, self.jobs, self.pid,
                {'kind':'cut', 'job_id':jid, 'time':4})

    def test_import_never_writes_source_and_revision_conflict(self):
        current, edl = self.draft()
        rev = self.store.save(self.pid, edl, current["revision"])
        self.assertNotEqual(rev, current["revision"])
        with self.assertRaises(Conflict):
            self.store.save(self.pid, edl, current["revision"])
        self.assertEqual([p.name for p in self.raw.iterdir()], ["photo.jpg"])
        self.assertTrue(
            (
                self.store.directory(self.pid)
                / "history"
                / f"{current['revision']}.json"
            ).exists()
        )

    def test_reject_external_paths_and_unsafe_model_edits(self):
        current, edl = self.draft()
        for field, value in [
            ("clip", "../../secret"),
            ("card", {"image": "/etc/passwd"}),
            ("in", float("nan")),
            ("speed", 0),
        ]:
            bad = copy.deepcopy(edl)
            bad["shots"][0][field] = value
            with self.assertRaises(ValueError):
                validate(bad, current["edl"], current["assets"])
        bad = copy.deepcopy(edl)
        bad["project"] = "/tmp/other"
        with self.assertRaises(ValueError):
            validate(bad, current["edl"], current["assets"])

    def test_old_effects_preserved_and_validated(self):
        current, edl = self.draft()
        self.store.save(self.pid, edl, current["revision"])
        current = self.store.load(self.pid)
        result = apply_proposal(
            current, {"summary": "缩短照片", "shots": [dict(edl["shots"][0], out=2)]}
        )
        self.assertEqual(result["shots"][0]["out"], 2)
        with self.assertRaises(ValueError):
            apply_proposal(
                current,
                {"summary": "x", "shots": [dict(edl["shots"][0], audio_src="/tmp/x")]},
            )

    def test_csrf_origin_and_host_checks(self):
        self.assertEqual(
            self.client.post(self.base + "/jobs", json={"kind": "scan"}).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/config", headers={"Host": "evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/config", headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        with self.client.get(self.base + "/assets/a0000/original") as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get(self.base + "/assets/unknown/original").status_code, 404
        )

    def test_scan_real_worker(self):
        response = self.post(self.base + "/jobs", {"kind": "scan"})
        self.assertEqual(response.status_code, 202)
        for _ in range(600):
            jobs = self.jobs.list(self.pid)
            if jobs[0]["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(jobs[0]["state"], "completed", jobs)
        data = self.store.load(self.pid)
        self.assertEqual(data["assets"][0]["width"], 80)
        with self.client.get(self.base + "/assets/a0000/thumbnail") as response:
            self.assertEqual(response.status_code, 200)

    def test_missing_credentials_is_not_queued_success(self):
        with patch.dict("os.environ", {}, clear=True):
            response = self.post(
                self.base + "/jobs",
                {
                    "kind": "agent",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "message": "剪辑",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.jobs.list(self.pid), [])

    def test_credentials_never_returned_or_persisted(self):
        with patch.dict("os.environ", {}, clear=True):
            response = self.post(
                "/api/model-credentials",
                {"provider": "openai", "key": "test-secret-only"},
            )
            self.assertEqual(response.status_code, 200)
            config = self.client.get("/api/config").json
            self.assertNotIn("test-secret-only", json.dumps(config))
            self.assertTrue(
                next(p for p in config["providers"] if p["id"] == "openai")[
                    "configured"
                ]
            )
            self.assertFalse(
                any(
                    "test-secret-only" in p.read_text()
                    for p in (self.root / "data").rglob("*.json")
                )
            )

    def test_no_empty_render(self):
        self.assertEqual(
            self.post(
                self.base + "/jobs", {"kind": "render", "mode": "final"}
            ).status_code,
            400,
        )

    def test_cancel_real_process(self):
        import subprocess, sys

        original = subprocess.Popen

        def slow_worker(*args, **kwargs):
            return original(
                [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
            )

        with patch("src.studio.app.subprocess.Popen", side_effect=slow_worker):
            job = self.jobs.start(self.pid, {"kind": "scan"})
        proc = self.jobs.processes[job["id"]]
        self.jobs.cancel(self.pid, job["id"])
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.poll())
        self.assertEqual(self.jobs.list(self.pid)[0]["state"], "cancelled")

    def test_restart_marks_incomplete_jobs_interrupted(self):
        from src.studio.store import write_json

        path = self.store.directory(self.pid) / "jobs" / ("a" * 32) / "job.json"
        write_json(
            path, {"id": "a" * 32, "state": "running", "created": 1, "kind": "agent"}
        )
        another = create_app(self.root / "data")
        self.assertEqual(
            another.extensions["studio_jobs"].list(self.pid)[0]["state"], "interrupted"
        )
        another.extensions["studio_jobs"].close()

    def test_mixed_camera_time_formats(self):
        from src.studio.worker import capture_time

        self.assertEqual(
            capture_time("2026:09:30 09:32:15", "x.jpg"), "2026-09-30T09:32:15"
        )
        self.assertEqual(
            capture_time("", "IMG20260930093215.jpg"), "2026-09-30T09:32:15"
        )
        self.assertLess(
            capture_time("2026:09:30 09:32:15", "x.jpg"),
            capture_time("2026-10-01T16:12:41", "x.mp4"),
        )
        self.assertEqual(capture_time("invalid", "unknown.jpg"), "")

    def test_reject_path_like_shot_ids(self):
        current, edl = self.draft()
        edl["shots"][0]["id"] = "../escape"
        with self.assertRaises(ValueError):
            validate(edl, current["edl"], current["assets"])

    def test_python_venv_path_is_preserved(self):
        import sys

        self.assertEqual(self.jobs.engine_python, str(Path(sys.executable).absolute()))

    def test_invalid_json_body(self):
        self.assertEqual(self.post(self.base + "/jobs", []).status_code, 400)
        self.assertEqual(
            self.client.put(
                self.base + "/edl", json={}, headers=self.headers
            ).status_code,
            400,
        )

    def test_apply_stale_proposal(self):
        from src.studio.store import write_json

        current, edl = self.draft()
        path = self.store.directory(self.pid) / "jobs" / ("b" * 32)
        write_json(path / "job.json", {"state": "completed", "kind": "agent"})
        write_json(
            path / "result.json",
            {
                "base_revision": current["revision"],
                "proposal": {"summary": "new", "shots": edl["shots"]},
            },
        )
        self.store.save(self.pid, edl, current["revision"])
        response = self.post(self.base + "/jobs/" + ("b" * 32) + "/apply", {})
        self.assertEqual(response.status_code, 409)

    def test_harness_no_shell_no_files_no_upload_plugin(self):
        config = patch_for("anthropic", "claude-sonnet-4-5")
        disabled = {r["id"] for r in config if r.get("disabled")}
        self.assertTrue(
            {"persistent-bash", "persistent-pwsh", "tool-fs", "session-log-deepseek"}
            <= disabled
        )
        self.assertEqual(
            config[-1]["insert"][0]["config"]["providers"]["anthropic"]["apiKeyEnv"],
            "ANTHROPIC_API_KEY",
        )


if __name__ == "__main__":
    unittest.main()
