"""Application-owned drafts, optimistic concurrency and immutable revisions."""

import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path
import threading
import uuid

MEDIA = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".m4v"}
PHOTO = {".jpg", ".jpeg", ".png"}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def revision(edl):
    return hashlib.sha256(
        json.dumps(edl, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def directory(self, pid):
        if len(pid) != 32 or any(c not in "0123456789abcdef" for c in pid):
            raise ValueError("无效的项目编号")
        path = self.root / "projects" / pid
        if not (path / "project.json").is_file():
            raise FileNotFoundError("项目不存在")
        return path

    def projects(self):
        return [
            read_json(p)
            for p in sorted((self.root / "projects").glob("*/project.json"))
        ]

    def load(self, pid):
        folder = self.directory(pid)
        edl = read_json(folder / "edl.json")
        return {
            "project": read_json(folder / "project.json"),
            "edl": edl,
            "revision": revision(edl),
            "assets": read_json(folder / "assets.json"),
        }

    def register(self, source):
        source = Path(source).expanduser().resolve(strict=True)
        if not source.is_dir():
            raise ValueError("请输入素材目录或视频项目目录")
        raw = (
            source / "01 - Unedited" if (source / "01 - Unedited").is_dir() else source
        )
        files = sorted(
            p for p in raw.iterdir() if p.is_file() and p.suffix.lower() in MEDIA
        )
        if not files:
            raise ValueError("目录中没有支持的照片或视频")
        pid = uuid.uuid4().hex
        folder = self.root / "projects" / pid
        assets = [
            {
                "id": f"a{i:04d}",
                "name": p.name,
                "path": str(p.resolve()),
                "kind": "photo" if p.suffix.lower() in PHOTO else "video",
            }
            for i, p in enumerate(files)
        ]
        old = source / "02 - Export/edit/edl.json"
        edl = (
            read_json(old)
            if old.exists()
            else {
                "project": str(source),
                "source_dir": str(raw),
                "output": {"width": 3840, "height": 2160, "fps": "30/1"},
                "shots": [],
            }
        )
        # Keep the original resolution base for existing auxiliary media. Original EDL is never written.
        edl["project"] = str(source)
        edl["source_dir"] = str(raw)
        project = {
            "id": pid,
            "name": source.name,
            "source": str(source),
            "raw": str(raw),
        }
        write_json(folder / "assets.json", assets)
        write_json(folder / "edl.json", edl)
        write_json(folder / "project.json", project)
        return pid

    def save(self, pid, edl, expected):
        with self.lock:
            current = self.load(pid)
            if expected != current["revision"]:
                raise Conflict("工程已经变化，请刷新后重新提交；现有修改未被覆盖")
            validate(edl, current["edl"], current["assets"])
            folder = self.directory(pid)
            write_json(
                folder / "history" / (current["revision"] + ".json"), current["edl"]
            )
            write_json(folder / "edl.json", edl)
            return revision(edl)


def number(value, name, low=0, high=86400):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} 不是有效数值")


def validate(edl, original, assets):
    if not isinstance(edl, dict) or set(edl) != set(original):
        raise ValueError("只允许修改镜头列表")
    for key in original:
        if key != "shots" and edl[key] != original[key]:
            raise ValueError(f"不允许修改工程字段：{key}")
    if not isinstance(edl["shots"], list) or len(edl["shots"]) > 2000:
        raise ValueError("无效的镜头列表")
    ids = set()
    originals = {s["id"]: s for s in original["shots"]}
    allowed = {"enabled", "in", "out", "fit", "zoom", "subs", "note", "speed"}
    asset_clips = {
        a["name"] if a["kind"] == "photo" else Path(a["name"]).stem for a in assets
    }
    for s in edl["shots"]:
        if (
            not isinstance(s, dict)
            or not isinstance(s.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", s["id"])
            or s["id"] in ids
        ):
            raise ValueError("镜头编号必须唯一")
        ids.add(s["id"])
        before = originals.get(s["id"])
        if before:
            if {k: v for k, v in s.items() if k not in allowed} != {
                k: v for k, v in before.items() if k not in allowed
            }:
                raise ValueError("不能改变现有镜头的素材或高级效果")
        elif (
            set(s) - allowed - {"id", "clip", "audio"}
            or s.get("clip") not in asset_clips
            or s.get("audio", "mute") not in ("voice", "ambient", "mute")
        ):
            raise ValueError("新镜头必须引用已导入素材，且不能包含高级执行参数")
        number(s.get("in"), "起点")
        number(s.get("out"), "终点")
        if s["out"] <= s["in"]:
            raise ValueError("镜头终点必须大于起点")
        number(s.get("speed", 1), "速度", 0.25, 8)
        if "enabled" in s and not isinstance(s["enabled"], bool):
            raise ValueError("镜头开关必须是布尔值")
        if "note" in s and (not isinstance(s["note"], str) or len(s["note"]) > 4000):
            raise ValueError("镜头说明过长")
        if (
            "fit" in s
            and s["fit"] not in ("auto", "cover", "blur")
            and (not before or s["fit"] != before.get("fit"))
        ):
            raise ValueError("无效的画幅模式")
        z = s.get("zoom")
        if z is not None:
            if not isinstance(z, dict) or set(z) - {"from", "to", "x", "y"}:
                raise ValueError("无效的缩放参数")
            for k in ("from", "to"):
                number(z.get(k, 1), k, 1, 4)
            for k in ("x", "y"):
                number(z.get(k, 0.5), k, 0, 1)
        if not isinstance(s.get("subs", []), list):
            raise ValueError("字幕必须是列表")
        for sub in s.get("subs", []):
            if not isinstance(sub, dict) or set(sub) - {
                "t0",
                "t1",
                "text",
                "kind",
                "pos",
            }:
                raise ValueError("无效的字幕")
            number(sub.get("t0"), "字幕起点")
            number(sub.get("t1"), "字幕终点")
            if (
                sub["t1"] <= sub["t0"]
                or not isinstance(sub.get("text"), str)
                or len(sub["text"]) > 1000
            ):
                raise ValueError("字幕时间或文字无效")
        asset = next(
            (
                a
                for a in assets
                if (a["name"] if a["kind"] == "photo" else Path(a["name"]).stem)
                == s.get("clip")
            ),
            None,
        )
        if asset and asset.get("duration") and s["out"] > asset["duration"] + 0.05:
            raise ValueError("镜头超出原视频时长")
    for entry in original.get("voiceover", []) + original.get("music", []):
        if entry.get("start") not in ids:
            raise ValueError("不能删除被音乐或旁白引用的镜头；可以关闭镜头")


def apply_proposal(current, proposal):
    if not isinstance(proposal, dict) or set(proposal) - {"summary", "shots"}:
        raise ValueError("模型返回的建议格式无效")
    if not isinstance(proposal.get("summary"), str) or not isinstance(
        proposal.get("shots"), list
    ):
        raise ValueError("模型必须返回说明和完整镜头列表")
    edl = copy.deepcopy(current["edl"])
    edl["shots"] = proposal["shots"]
    validate(edl, current["edl"], current["assets"])
    return edl


def proposal_changes(edl, proposal):
    """Derive executable changes from data, never from the model's prose."""
    before = edl['shots']
    after = proposal['shots']
    old = {s['id']: s for s in before}
    new = {s['id']: s for s in after}
    changes = []
    for sid in old.keys() - new.keys():
        changes.append({'shot_id': sid, 'field': '镜头', 'before': old[sid], 'after': None})
    for shot in after:
        sid = shot['id']
        if sid not in old:
            changes.append({'shot_id':sid, 'field':'镜头', 'before':None, 'after':shot})
        else:
            for field in sorted(old[sid].keys() | shot.keys()):
                if old[sid].get(field) != shot.get(field):
                    changes.append({'shot_id':sid, 'field':field, 'before':old[sid].get(field), 'after':shot.get(field)})
    if [s['id'] for s in before if s['id'] in new] != [s['id'] for s in after if s['id'] in old]:
        changes.append({'shot_id':'', 'field':'顺序', 'before':[s['id'] for s in before], 'after':[s['id'] for s in after]})
    return changes
