"""Single-user, loopback-only video workbench. Run: python -m src.studio.app."""

import argparse
import atexit
import importlib.util
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
import uuid

from flask import Flask, abort, jsonify, request, send_file
from .store import Store, Conflict, read_json, write_json, apply_proposal
from .harness import PROVIDERS
from .context import resolve_context

REPO = Path(__file__).resolve().parents[2]


class Jobs:
    def __init__(self, store, engine_root, engine_python):
        self.store, self.engine_root, self.engine_python = (
            store,
            engine_root,
            engine_python,
        )
        self.processes = {}
        self.lock = threading.RLock()
        # Do not pretend jobs survive a server restart. A fresh server owns no old processes.
        for path in store.root.glob("projects/*/jobs/*/job.json"):
            data = read_json(path)
            if data["state"] in ("queued", "running"):
                data.update(state="interrupted", error="服务已重启；请重新提交任务")
                write_json(path, data)

    def list(self, pid):
        with self.lock:
            result = []
            for file in self.store.directory(pid).glob("jobs/*/job.json"):
                data = read_json(file)
                for extra in ("result", "error", "progress"):
                    p = file.parent / (extra + ".json")
                    if p.exists():
                        data[extra] = read_json(p)
                location = file.parent / "render-location.json"
                if data["state"] == "running" and location.exists():
                    status = Path(read_json(location)["edit"]) / "render_status.json"
                    try:
                        data["progress"] = read_json(status)
                    except (FileNotFoundError, ValueError):
                        pass
                result.append(data)
            return sorted(result, key=lambda j: j["created"], reverse=True)

    def start(self, pid, payload):
        with self.lock:
            if any(
                j["state"] in ("queued", "running") or j["id"] in self.processes
                for j in self.list(pid)
            ):
                raise Conflict("该项目已有任务运行，请等待或取消后再提交")
            jid = uuid.uuid4().hex
            folder = self.store.directory(pid)
            job = folder / "jobs" / jid
            current = self.store.load(pid)
            payload = dict(
                payload, engine_root=self.engine_root, engine_python=self.engine_python
            )
            write_json(job / "request.json", payload)
            write_json(job / "input-edl.json", current["edl"])
            meta = {
                "id": jid,
                "kind": payload["kind"],
                "state": "running",
                "created": time.time(),
                "base_revision": current["revision"],
                "message": payload.get("message", ""),
                "context": payload.get("context"),
            }
            write_json(job / "job.json", meta)
            try:
                log = open(job / "worker.log", "wb")
                try:
                    proc = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "src.studio.worker",
                            str(folder),
                            str(job),
                        ],
                        cwd=REPO,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                    )
                finally:
                    log.close()
            except Exception:
                meta.update(state="failed", error="无法启动任务进程")
                write_json(job / "job.json", meta)
                raise
            self.processes[jid] = proc
            threading.Thread(target=self._wait, args=(proc, job), daemon=True).start()
            return meta

    def _wait(self, proc, job):
        code = proc.wait()
        with self.lock:
            if not (job / "job.json").exists():
                return
            meta = read_json(job / "job.json")
            if meta["state"] == "running":
                meta.update(
                    state="completed"
                    if code == 0 and (job / "result.json").exists()
                    else "failed",
                    exit_code=code,
                    finished=time.time(),
                )
                write_json(job / "job.json", meta)
            self.processes.pop(meta["id"], None)

    def cancel(self, pid, jid):
        with self.lock:
            job = self.jobdir(pid, jid)
            proc = self.processes.get(jid)
            if proc and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                meta = read_json(job / "job.json")
                meta["state"] = "cancelled"
                write_json(job / "job.json", meta)

                def reap():
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

                threading.Thread(target=reap, daemon=True).start()
            return {"ok": True}

    def jobdir(self, pid, jid):
        if len(jid) != 32 or any(c not in "0123456789abcdef" for c in jid):
            raise ValueError("无效的任务编号")
        path = self.store.directory(pid) / "jobs" / jid
        if not (path / "job.json").exists():
            raise FileNotFoundError("任务不存在")
        return path

    def close(self):
        with self.lock:
            for proc in list(self.processes.values()):
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass


def create_app(data_root, engine_root=REPO, engine_python=sys.executable):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=8 * 1024 * 1024)
    store = Store(data_root)
    jobs = Jobs(
        store, str(Path(engine_root).resolve()), str(Path(engine_python).absolute())
    )
    token = secrets.token_urlsafe(32)
    app.extensions.update(studio_store=store, studio_jobs=jobs, studio_token=token)
    atexit.register(jobs.close)

    @app.before_request
    def local_only():
        if request.remote_addr not in ("127.0.0.1", "::1", None):
            abort(403)
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403)
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(
                request.headers.get("X-Studio-Token", ""), token
            ):
                abort(403)
            if not request.is_json:
                abort(415)
            if not isinstance(request.get_json(), dict):
                raise ValueError("请求必须是 JSON 对象")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        )
        return response

    @app.errorhandler(ValueError)
    def bad(exc):
        return jsonify(error=str(exc)), 409 if isinstance(exc, Conflict) else 400

    @app.errorhandler(FileNotFoundError)
    def missing(exc):
        return jsonify(error=str(exc)), 404

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/config")
    def config():
        return jsonify(
            token=token,
            providers=[
                {"id": p, "configured": bool(os.environ.get(key)), "env": key}
                for p, key in PROVIDERS.items()
            ],
            harness_installed=importlib.util.find_spec("deepseek_harness") is not None,
            engine_root=str(engine_root),
            data_root=str(store.root),
        )

    @app.post("/api/model-credentials")
    def model_credentials():
        data = request.get_json()
        provider, key = data.get("provider"), data.get("key")
        if (
            provider not in PROVIDERS
            or not isinstance(key, str)
            or not key.strip()
            or len(key) > 4096
        ):
            raise ValueError("请选择模型服务并填写有效密钥")
        os.environ[PROVIDERS[provider]] = key.strip()
        return jsonify(configured=True)

    @app.get("/api/projects")
    def projects():
        return jsonify(store.projects())

    @app.post("/api/projects")
    def add_project():
        data = request.get_json()
        if not isinstance(data.get("path"), str):
            raise ValueError("请填写本机素材目录")
        with store.lock:
            pid = store.register(data["path"])
        job = jobs.start(pid, {"kind": "scan"})
        return jsonify(id=pid, job=job), 201

    @app.get("/api/projects/<pid>")
    def project(pid):
        return jsonify(store.load(pid))

    @app.put("/api/projects/<pid>/edl")
    def save_edl(pid):
        data = request.get_json()
        if not isinstance(data, dict) or "edl" not in data:
            raise ValueError("缺少剪辑工程")
        with jobs.lock:
            if any(
                j["state"] == "running" and j["kind"] == "scan" for j in jobs.list(pid)
            ):
                raise Conflict("素材整理完成后才能修改工程")
            rev = store.save(pid, data["edl"], data.get("revision"))
        return jsonify(revision=rev)

    @app.get("/api/projects/<pid>/assets/<aid>/<kind>")
    def asset(pid, aid, kind):
        data = store.load(pid)
        a = next((a for a in data["assets"] if a["id"] == aid), None)
        if not a:
            abort(404)
        if kind == "thumbnail":
            path = store.directory(pid) / "thumbs" / (aid + ".jpg")
        elif kind == "original":
            path = Path(a["path"])
        else:
            abort(404)
        return send_file(path, conditional=True)

    @app.get("/api/projects/<pid>/jobs")
    def list_jobs(pid):
        return jsonify(jobs.list(pid))

    @app.post("/api/projects/<pid>/jobs")
    def start_job(pid):
        data = request.get_json()
        kind = data.get("kind")
        if kind == "agent":
            if (
                data.get("provider") not in PROVIDERS
                or not isinstance(data.get("model"), str)
                or not data["model"].strip()
            ):
                raise ValueError("请选择模型服务并填写模型名称")
            if not os.environ.get(PROVIDERS[data["provider"]]):
                raise ValueError(f"请配置 {PROVIDERS[data['provider']]} 后重启服务")
            if (
                not isinstance(data.get("message"), str)
                or not 1 <= len(data["message"].strip()) <= 10000
            ):
                raise ValueError("请输入 1 至 10000 字的修改要求")
            if data.get("base_url") and (
                not isinstance(data["base_url"], str)
                or urlparse(data["base_url"]).scheme not in ("https", "http")
            ):
                raise ValueError("服务地址必须是 HTTP 或 HTTPS 地址")
            if data.get("context") is not None:
                context, _ = resolve_context(store, jobs, pid, data["context"])
                data["context"] = context
            if not isinstance(data.get("asset_ids", []), list):
                raise ValueError("素材选择无效")
        elif kind == "render":
            if data.get("mode") not in ("preview", "final"):
                raise ValueError("导出模式无效")
            if not any(s.get("enabled", True) for s in store.load(pid)["edl"]["shots"]):
                raise ValueError("请先添加镜头或应用剪辑建议")
        elif kind != "scan":
            raise ValueError("任务类型无效")
        return jsonify(jobs.start(pid, data)), 202

    @app.post("/api/projects/<pid>/jobs/<jid>/cancel")
    def cancel(pid, jid):
        return jsonify(jobs.cancel(pid, jid))

    @app.post("/api/projects/<pid>/jobs/<jid>/apply")
    def apply(pid, jid):
        with store.lock:
            path = jobs.jobdir(pid, jid)
            if read_json(path / "job.json")["state"] != "completed":
                raise Conflict("建议尚未完成")
            result = read_json(path / "result.json")
            if "proposal" not in result:
                raise ValueError("该任务没有剪辑建议")
            current = store.load(pid)
            edl = apply_proposal(current, result["proposal"])
            rev = store.save(pid, edl, result["base_revision"])
        return jsonify(revision=rev)

    @app.get("/api/projects/<pid>/jobs/<jid>/output")
    def output(pid, jid):
        path = jobs.jobdir(pid, jid)
        if read_json(path / "job.json")["state"] != "completed":
            abort(404)
        result = read_json(path / "result.json")
        if "output" not in result:
            abort(404)
        target = Path(result["output"]).resolve()
        if not target.is_relative_to(path.resolve()):
            abort(403)
        return send_file(
            target, conditional=True, as_attachment=request.args.get("download") == "1"
        )

    @app.get("/api/projects/<pid>/playback")
    def playback(pid):
        finished = [j for j in jobs.list(pid) if j["state"] == "completed" and j.get("result", {}).get("mode") == "preview"]
        jid = finished[0]["id"] if finished else None
        return jsonify(job_id=jid, url=f"/api/projects/{pid}/jobs/{jid}/output" if jid else f"/api/projects/{pid}/preview")

    @app.get("/api/projects/<pid>/preview")
    def preview(pid):
        finished = [
            j
            for j in jobs.list(pid)
            if j["state"] == "completed"
            and j.get("result", {}).get("mode") == "preview"
        ]
        if finished:
            return output(pid, finished[0]["id"])
        source = (
            Path(store.load(pid)["project"]["source"]) / "02 - Export/edit/preview.mp4"
        )
        if not source.exists():
            abort(404)
        return send_file(source, conditional=True)

    return app


def main():
    ap = argparse.ArgumentParser(description="本地视频工作台")
    ap.add_argument("--data", default=str(Path.home() / ".local/share/video-studio"))
    ap.add_argument("--engine-root", default=str(REPO))
    ap.add_argument("--engine-python", default=sys.executable)
    ap.add_argument("--port", type=int, default=8770)
    args = ap.parse_args()
    # One service owner per data directory, so startup cannot interrupt a live owner's jobs.
    import fcntl

    root = Path(args.data).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lock = open(root / "server.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("此数据目录已有工作台运行")
    app = create_app(root, args.engine_root, args.engine_python)

    def shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    try:
        app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
    finally:
        app.extensions["studio_jobs"].close()
        lock.close()


if __name__ == "__main__":
    main()
