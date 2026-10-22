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
