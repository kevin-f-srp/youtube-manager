"""Resolve review anchors using registered media and immutable render inputs."""

import math
from pathlib import Path
from .store import read_json, revision


def segments(shot):
    spans = [(shot["in"], shot["out"])]
    for left, right in shot.get("skip", []) if shot.get("skip_on") is not False else []:
        spans = [(a, min(b, left)) for a, b in spans if a < left] + [
            (max(a, right), b) for a, b in spans if b > right
        ]
    return sorted((a, b) for a, b in spans if b > a)


def resolve_context(store, jobs, pid, anchor):
    if not isinstance(anchor, dict):
        raise ValueError("画面上下文格式无效")
    t = anchor.get("time", 0)
    if (
        isinstance(t, bool)
        or not isinstance(t, (int, float))
        or not math.isfinite(t)
        or t < 0
    ):
        raise ValueError("画面时间无效")
    current = store.load(pid)
    context = {"kind": anchor.get("kind"), "time": t, "shot_ids": []}
    edl = None
    if context["kind"] == "source":
        asset = next(
            (a for a in current["assets"] if a["id"] == anchor.get("asset_id")), None
        )
        if not asset:
            raise ValueError("画面素材不存在")
        if asset["kind"] == "video" and t >= asset.get("duration", float("inf")):
            raise ValueError("画面时间超出素材时长")
        context.update(
            asset_id=asset["id"], name=asset["name"], media_kind=asset["kind"]
        )
        for s in current["edl"]["shots"]:
            if s.get("enabled") is not False and s.get("clip") in (
                asset["name"],
                Path(asset["name"]).stem,
            ):
                if asset["kind"] == "photo" or any(a <= t < b for a, b in segments(s)):
                    context["shot_ids"].append(s["id"])
        target = Path(asset["path"])
    elif context["kind"] == "cut":
        jid = anchor.get("job_id")
        if jid:
            if not isinstance(jid, str):
                raise ValueError("任务编号无效")
            folder = jobs.jobdir(pid, jid)
            result = read_json(folder / "result.json")
            if (
                read_json(folder / "job.json")["state"] != "completed"
                or "output" not in result
            ):
                raise ValueError("播放任务尚未完成")
            target = Path(result["output"]).resolve()
            if not target.is_relative_to(folder.resolve()):
                raise ValueError("播放文件路径无效")
            if t >= result["duration"]:
                raise ValueError("画面时间超出成片时长")
            edl = read_json(
                folder
                / (
                    "preview-edl.json"
                    if (folder / "preview-edl.json").exists()
                    else "input-edl.json"
                )
            )
            context.update(job_id=jid, revision=revision(edl))
        else:
            target = Path(current["project"]["source"]) / "02 - Export/edit/preview.mp4"
            context["mapping_note"] = (
                "导入的旧预览没有可靠的剪辑快照，仅以实际画面和成片时间定位。"
            )
        if edl:
            cursor = 0
            for shot in edl["shots"]:
                if shot.get("enabled") is False:
                    continue
                for a, b in segments(shot):
                    length = (b - a) / shot.get("speed", 1)
                    if cursor <= t < cursor + length:
                        context.update(
                            shot_ids=[shot["id"]],
                            clip=shot.get("clip"),
                            source_time=a + (t - cursor) * shot.get("speed", 1),
                        )
                    cursor += length
    else:
        raise ValueError("请选择原素材或成片画面")
    if not target.is_file():
        raise ValueError("画面文件不存在，请先生成预览")
    return context, target
