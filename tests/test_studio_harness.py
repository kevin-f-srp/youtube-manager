import json, os, threading, tempfile, unittest
from unittest.mock import patch
from PIL import Image
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from src.studio.harness import propose


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.captured = {"path": self.path, "data": data}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        answer = json.dumps({"summary": "测试响应", "shots": []}, ensure_ascii=False)
        if "messages" in self.path:
            events = [
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_test",
                            "type": "message",
                            "role": "assistant",
                            "model": "claude-sonnet-4-5",
                            "content": [],
                            "stop_reason": None,
                            "usage": {"input_tokens": 10, "output_tokens": 0},
                        },
                    },
                ),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": answer},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 20},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            for event, obj in events:
                self.wfile.write(
                    ("event: " + event + "\ndata: " + json.dumps(obj) + "\n\n").encode()
                )
        elif "responses" in self.path:
            response = {
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "in_progress",
                "model": "gpt-4o",
                "output": [],
            }
            item = {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
            events = [
                {"type": "response.created", "response": response},
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {
                    "type": "response.content_part.added",
                    "output_index": 0,
                    "item_id": "msg_test",
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "item_id": "msg_test",
                    "content_index": 0,
                    "delta": answer,
                },
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "item_id": "msg_test",
                    "content_index": 0,
                    "text": answer,
                },
            ]
            item = dict(
                item,
                status="completed",
                content=[{"type": "output_text", "text": answer, "annotations": []}],
            )
            events += [
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {
                    "type": "response.completed",
                    "response": dict(
                        response,
                        status="completed",
                        output=[item],
                        usage={
                            "input_tokens": 10,
                            "output_tokens": 20,
                            "total_tokens": 30,
                        },
                    ),
                },
            ]
            for obj in events:
                self.wfile.write(
                    (
                        "event: " + obj["type"] + "\ndata: " + json.dumps(obj) + "\n\n"
                    ).encode()
                )
        else:
            for delta, finish in [
                ({"role": "assistant", "content": answer}, None),
                ({}, "stop"),
            ]:
                obj = {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")


@unittest.skipUnless(
    os.environ.get("STUDIO_HARNESS_TESTS") == "1", "explicit SDK integration opt-in"
)
class HarnessProtocolTests(unittest.TestCase):
    def test_gpt_and_claude_images_without_tools(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for provider, model, key in [
                ("openai", "gpt-4o", "OPENAI_API_KEY"),
                ("anthropic", "claude-sonnet-4-5", "ANTHROPIC_API_KEY"),
            ]:
                with (
                    self.subTest(provider=provider),
                    tempfile.TemporaryDirectory() as temp,
                    patch.dict(os.environ, {key: "test-only"}),
                ):
                    image = Path(temp) / "test.jpg"
                    Image.new("RGB", (40, 40), "green").save(image)
                    base = f"http://127.0.0.1:{server.server_port}" + (
                        "/v1" if provider == "openai" else ""
                    )
                    result = propose(
                        Path(temp) / "home",
                        provider,
                        model,
                        "Return JSON",
                        images=[(f"sheet-{i}.jpg", image) for i in range(17)],
                        base_url=base,
                    )
                    self.assertEqual(result["shots"], [])
                    self.assertEqual(server.captured["data"].get("tools", []), [])
                    payload = server.captured["data"]
                    blocks = [
                        block
                        for msg in payload.get("input", payload.get("messages", []))
                        if isinstance(msg, dict)
                        for block in msg.get("content", [])
                        if isinstance(block, dict)
                    ]
                    self.assertEqual(
                        sum(
                            block.get("type") in ("image", "input_image")
                            for block in blocks
                        ),
                        17,
                    )
                    if provider == "anthropic":
                        self.assertTrue(
                            server.captured["path"].startswith("/v1/messages")
                        )
                    else:
                        self.assertEqual(server.captured["path"], "/v1/responses")
        finally:
            server.shutdown()
            server.server_close()


class ToolHandler(Handler):
    """A model double that requests a real project tool, then sees its image result."""

    def do_POST(self):
        import io

        body = self.rfile.read(int(self.headers["Content-Length"]))
        data = json.loads(body)
        self.server.requests.append(data)
        if len(self.server.requests) != 1:
            self.rfile = io.BytesIO(body)
            return super().do_POST()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        arguments = {"action": "frame", "args": json.dumps({"asset_id": "a0000"})}
        if "messages" in self.path:
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_tool",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-sonnet-4-5",
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool_test",
                        "name": "studio",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(arguments),
                    },
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 10},
                },
                {"type": "message_stop"},
            ]
        else:
            item = {
                "id": "fc_test",
                "type": "function_call",
                "call_id": "call_test",
                "name": "studio",
                "arguments": json.dumps(arguments),
                "status": "completed",
            }
            response = {
                "id": "resp_tool",
                "object": "response",
                "created_at": 1,
                "status": "in_progress",
                "model": "gpt-4o",
                "output": [],
            }
            events = [
                {"type": "response.created", "response": response},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": dict(item, arguments="", status="in_progress"),
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": item["id"],
                    "output_index": 0,
                    "delta": item["arguments"],
                },
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": 0,
                    "arguments": item["arguments"],
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {
                    "type": "response.completed",
                    "response": dict(
                        response,
                        status="completed",
                        output=[item],
                        usage={
                            "input_tokens": 10,
                            "output_tokens": 10,
                            "total_tokens": 20,
                        },
                    ),
                },
            ]
        for event in events:
            self.wfile.write(
                (
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                ).encode()
            )


@unittest.skipUnless(
    os.environ.get("STUDIO_HARNESS_TESTS") == "1", "explicit SDK integration opt-in"
)
class PersistentAgentTests(unittest.TestCase):
    def test_actual_tools_image_and_resume_for_both_providers(self):
        from src.studio.harness import run_agent
        from src.studio.store import Store, write_json, read_json
        import sys

        for provider, model, key in [
            ("openai", "gpt-4o", "OPENAI_API_KEY"),
            ("anthropic", "claude-sonnet-4-5", "ANTHROPIC_API_KEY"),
        ]:
            server = ThreadingHTTPServer(("127.0.0.1", 0), ToolHandler)
            server.requests = []
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with (
                    self.subTest(provider=provider),
                    tempfile.TemporaryDirectory() as temp,
                    patch.dict(os.environ, {key: "test-only"}),
                ):
                    p = Path(temp)
                    raw = p / "raw"
                    raw.mkdir()
                    Image.new("RGB", (40, 40), "red").save(raw / "image.jpg")
                    store = Store(p / "state")
                    pid = store.register(raw)
                    folder = store.directory(pid)
                    job = folder / "jobs" / ("a" * 32)
                    job.mkdir(parents=True)
                    write_json(
                        job / "request.json",
                        {
                            "engine_root": str(Path(__file__).resolve().parents[1]),
                            "engine_python": sys.executable,
                        },
                    )
                    base = f"http://127.0.0.1:{server.server_port}" + (
                        "/v1" if provider == "openai" else ""
                    )
                    notifications = []
                    run_agent(
                        folder / "agent",
                        folder,
                        job,
                        provider,
                        model,
                        "Remember the red image",
                        base_url=base,
                        on_notification=notifications.append,
                    )
                    self.assertEqual(len(server.requests), 2)
                    results = [
                        n.payload["event"]
                        for n in notifications
                        if n.payload.get("event", {}).get("type") == "tool/result"
                    ]
                    self.assertFalse(results[0]["data"]["message"]["isError"])
                    self.assertTrue(
                        any(
                            b["type"] == "image"
                            for b in results[0]["data"]["message"]["content"]
                        )
                    )
                    first = read_json(folder / "agent/session.json")["session_id"]
                    run_agent(
                        folder / "agent",
                        folder,
                        job,
                        provider,
                        model,
                        "Continue editing",
                        base_url=base,
                    )
                    self.assertEqual(
                        read_json(folder / "agent/session.json")["session_id"], first
                    )
                    self.assertIn(
                        "Remember the red image", json.dumps(server.requests[-1])
                    )
                    self.assertEqual(
                        len(list((folder / "generated").glob("frame-*.jpg"))), 1
                    )
            finally:
                server.shutdown()
                server.server_close()

    def test_graceful_cancel_preserves_session_for_next_turn(self):
        import signal, subprocess, sys
        from src.studio.harness import run_agent
        from src.studio.store import Store, write_json, read_json

        class SlowFirst(Handler):
            def do_POST(self):
                if not self.server.seen.is_set():
                    self.server.seen.set()
                    self.server.release.wait(10)
                    return
                super().do_POST()

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowFirst)
        server.seen = threading.Event()
        server.release = threading.Event()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        proc = None
        try:
            with (
                tempfile.TemporaryDirectory() as temp,
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}),
            ):
                p = Path(temp)
                raw = p / "raw"
                raw.mkdir()
                Image.new("RGB", (40, 40)).save(raw / "a.jpg")
                store = Store(p / "state")
                folder = store.directory(store.register(raw))
                job = folder / "jobs" / ("a" * 32)
                job.mkdir(parents=True)
                write_json(
                    job / "request.json",
                    {"engine_root": str(Path.cwd()), "engine_python": sys.executable},
                )
                base = f"http://127.0.0.1:{server.server_port}/v1"
                code = "from pathlib import Path;from src.studio.harness import run_agent;import sys;p=Path(sys.argv[1]);j=Path(sys.argv[2]);run_agent(p/'agent',p,j,'openai','gpt-4o','start interrupted turn',base_url=sys.argv[3])"
                proc = subprocess.Popen(
                    [sys.executable, "-c", code, str(folder), str(job), base],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.assertTrue(server.seen.wait(10))
                sid = read_json(folder / "agent/session.json")["session_id"]
                os.kill(proc.pid, signal.SIGTERM)
                proc.wait(timeout=15)
                result = run_agent(
                    folder / "agent",
                    folder,
                    job,
                    "openai",
                    "gpt-4o",
                    "continue after cancel",
                    base_url=base,
                )
                self.assertIn("测试响应", result)
                self.assertEqual(
                    sid, read_json(folder / "agent/session.json")["session_id"]
                )
        finally:
            if proc is not None and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            server.release.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
