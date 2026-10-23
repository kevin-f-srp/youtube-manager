"""Project-scoped editing tools called by DSH. Never writes source projects."""

import copy
import contextlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid
from .store import Store, read_json, write_json, revision

HELP = {
    "project": "Read current EDL, assets and revision. Optional shot_id restricts shots.",
    "files": "List original edit files or engine source. args: area=edit|engine, prefix optional.",
    "read": "Read a text file. args: area=edit|engine, path relative, offset=0, limit=12000.",
    "edit_shot": "Update one shot without resending all shots. args: revision, shot_id, changes object (in,out,enabled,speed,fit,zoom,subs,note). Read project with shot_id first.",
    "edit_shots": "Apply actual shot changes to the studio copy and back up old EDL. args: revision, shots=complete shot list. Use project first.",
    "maps": "Discover map configs and matching card shots, with actual font settings.",
    "fonts": "List locally available font paths accepted by map editing.",
    "map_update": "Create a versioned map config. args: config relative map JSON path returned by maps; changes object: fonts {heavy,sans}, title_size,name_size,sub_size,number_size,sub_title_size,flight_label_size, labels, title, sub_title, colors. Read existing config first; no video is changed yet.",
    "map_preview": "Generate a still image from a map config and return image to inspect. args: config (source path or generated config id).",
    "map_render": "Render map video, fully decode-check, and update a matching card shot in studio EDL. args: config generated config id, shot_id, revision. Keeps original video and earlier generated versions.",
    "frame": "Inspect source or generated media frame. args: asset_id OR artifact relative path; time seconds. Returns image.",
    "render_preview": "Render current whole project preview and run existing audio/decode checks. Returns downloadable artifact. Can take minutes.",
    "history": "List saved EDL revisions.",
    "restore": "Restore a saved EDL revision to current studio project; keeps current revision in history. args: revision. Then render_preview to update visible video.",
}


def inside(root, name):
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError("路径必须位于当前工程范围内")
    return path


def fonts(engine):
    roots = [
        engine / "assets/fonts",
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path.home() / "Library/Fonts",
        Path("/usr/share/fonts"),
    ]
    return sorted(
        {
            str(p.resolve())
            for root in roots
            if root.exists()
            for p in root.rglob("*")
            if p.suffix.lower() in (".otf", ".ttf", ".ttc")
        }
    )


def source_config(folder, edit, name):
    if name.startswith("generated/"):
        return inside(folder, name)
    p = inside(edit, name)
    if p.suffix != ".json" or "maps" not in p.relative_to(edit).parts:
        raise ValueError("请选择 maps 中的地图配置")
    return p


def trusted_save(folder, old, new):
    # Caller has already validated the operation. Recheck after long renders.
    if revision(read_json(folder / "edl.json")) != revision(old):
        raise ValueError("工程在执行期间发生变化，请重新读取工程后操作")
    write_json(folder / "history" / f"{revision(old)}.json", old)
    write_json(folder / "edl.json", new)
    return revision(new)


def execute(folder, job, action, args):
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    store = Store(folder.parent.parent)
    current = store.load(folder.name)
    request = read_json(job / "request.json")
    engine = Path(request["engine_root"]).resolve()
    edit = (Path(current["project"]["source"]) / "02 - Export/edit").resolve()
    edl = current["edl"]
    if action == "help":
        return HELP
    if action == "project":
        result = copy.deepcopy(current)
        if args.get("shot_id"):
            result["edl"]["shots"] = [
                s for s in edl["shots"] if s["id"] == args["shot_id"]
            ]
        result["assets"] = [
            {k: v for k, v in a.items() if k != "path"} for a in current["assets"]
        ]
        result["maps_available"] = list(
            str(p.relative_to(edit)) for p in (edit / "maps").glob("*.json")
        )
        return result
    if action in ("files", "read"):
        area = args.get("area", "edit")
        if area not in ("edit", "engine"):
            raise ValueError("area 只能是 edit 或 engine")
        root = edit if area == "edit" else engine / "src/editor"
        if action == "files":
            target = inside(root, args.get("prefix", ""))
            return {
                "files": [
                    str(p.relative_to(root))
                    for p in target.rglob("*")
                    if p.is_file()
                    and p.suffix.lower() in (".json", ".py", ".md", ".txt", ".srt")
                ][:1000]
            }
        path = inside(root, args["path"])
        if (
            path.suffix.lower() not in (".json", ".py", ".md", ".txt", ".srt")
            or path.stat().st_size > 8_000_000
        ):
            raise ValueError("该文件不是可读取的工程文本；视频请用 frame 查看")
        text = path.read_text()
        offset = max(0, int(args.get("offset", 0)))
        limit = min(20000, max(1, int(args.get("limit", 12000))))
        return {
            "path": args["path"],
            "text": text[offset : offset + limit],
            "total_chars": len(text),
        }
    if action == "fonts":
        return {"fonts": fonts(engine)}
    if action == "edit_shot":
        new = copy.deepcopy(edl)
        shot = next((s for s in new["shots"] if s["id"] == args["shot_id"]), None)
        if not shot or not isinstance(args.get("changes"), dict):
            raise ValueError("镜头不存在或修改参数无效")
        shot.update(args["changes"])
        return {
            "revision": store.save(folder.name, new, args["revision"]),
            "saved": True,
            "shot_id": args["shot_id"],
        }
    if action == "edit_shots":
        new = copy.deepcopy(edl)
        new["shots"] = args["shots"]
        return {
            "revision": store.save(folder.name, new, args["revision"]),
            "saved": True,
        }
    if action == "maps":
        configs = []
        for p in (edit / "maps").glob("*.json"):
            c = read_json(p)
            if "stops" not in c or "bbox" not in c:
                continue
            output = (p.parent / c.get("out", "route.mp4")).resolve()
            matching = [
                s["id"]
                for s in edl["shots"]
                if s.get("card", {}).get("image")
                and (edit / s["card"]["image"]).resolve() == output
            ]
            configs.append(
                {
                    "config": str(p.relative_to(edit)),
                    "shot_ids": matching,
                    "settings": c,
                }
            )
        generated = []
        for p in (folder / "generated").glob("map-*/config.json"):
            generated.append(
                {"config": str(p.relative_to(folder)), "settings": read_json(p)}
            )
        return {"source_configs": configs, "generated_configs": generated}
    if action == "map_update":
        src = source_config(folder, edit, args["config"])
        cfg = read_json(src)
        allowed = {
            "fonts",
            "title_size",
            "name_size",
            "sub_size",
            "number_size",
            "sub_title_size",
            "flight_label_size",
            "labels",
            "title",
            "sub_title",
            "colors",
        }
        changes = args["changes"]
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise ValueError("地图修改仅接受 help 中声明的样式字段")
        cfg.update(changes)
        for key in allowed:
            if key.endswith("_size") and key in cfg:
                v = cfg[key]
                if (
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or not 8 <= v <= 500
                ):
                    raise ValueError("地图字号必须为 8 至 500")
        if not isinstance(cfg.get("fonts", {}), dict) or set(cfg.get("fonts", {})) - {
            "heavy",
            "sans",
        }:
            raise ValueError("字体配置只能包含 heavy 和 sans")
        valid_fonts = fonts(engine)
        for value in cfg.get("fonts", {}).values():
            if value not in valid_fonts:
                raise ValueError("字体必须从 fonts 返回的已安装字体中选择")
        osm = (src.parent / cfg["osm"]).resolve()
        if (
            not (osm.is_relative_to(edit) or osm.is_relative_to(folder))
            or not osm.is_file()
        ):
            raise ValueError("地图缓存数据不在项目内或不存在")
        generated = folder / "generated" / ("map-" + uuid.uuid4().hex)
        generated.mkdir(parents=True)
        cfg.update(osm=str(osm), out="map.mp4", preview="preview.jpg")
        cfg.pop("_dir", None)
        path = generated / "config.json"
        write_json(path, cfg)
        return {
            "config": str(path.relative_to(folder)),
            "changes": changes,
            "note": "配置已保存，尚未更新成片；先 map_preview 检查，再 map_render 更新指定镜头",
        }
    if action in ("map_preview", "map_render"):
        config = source_config(folder, edit, args["config"])
        if action == "map_render" and not config.is_relative_to(folder):
            raise ValueError("请先 map_update 创建项目副本中的地图配置")
        if action == "map_render" and args.get("revision") != current["revision"]:
            raise ValueError("剪辑方案已变化，请重新读取 project")
        dest = folder / "generated" / ("map-output-" + uuid.uuid4().hex)
        dest.mkdir(parents=True)
        # Invoke the existing route renderer in the established engine environment.
        script = "from src.editor.routemap import RouteMap,render_route; import sys; "
        if action == "map_preview":
            script += "r=RouteMap(sys.argv[1],verbose=False); r.frame(r.cfg['dur']-.05).resize((1280,720)).save(sys.argv[2])"
            argv = [str(config), str(dest / "preview.jpg")]
        else:
            script += "render_route(sys.argv[1],out=sys.argv[2],preview=sys.argv[3])"
            argv = [str(config), str(dest / "map.mp4"), str(dest / "preview.jpg")]
        with (dest / "render.log").open("w") as log:
            subprocess.run(
                [request["engine_python"], "-c", script, *argv],
                cwd=engine,
                stdout=log,
                stderr=log,
                check=True,
            )
        if action == "map_render":
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(dest / "map.mp4"),
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            new = copy.deepcopy(edl)
            shot = next((s for s in new["shots"] if s["id"] == args["shot_id"]), None)
            if not shot or not isinstance(shot.get("card"), dict):
                raise ValueError("指定镜头不是地图/标题卡")
            # Generated card path is trusted and immutable; source paths remain untouched.
            shot["card"]["image"] = str(dest / "map.mp4")
            rev = trusted_save(folder, edl, new)
        else:
            rev = current["revision"]
        return {
            "image": str(dest / "preview.jpg"),
            "artifact": str(
                (
                    dest / ("map.mp4" if action == "map_render" else "preview.jpg")
                ).relative_to(folder)
            ),
            "revision": rev,
            "applied": action == "map_render",
        }
    if action == "frame":
        from PIL import Image, ImageOps

        if args.get("asset_id"):
            a = next(a for a in current["assets"] if a["id"] == args["asset_id"])
            source = Path(a["path"])
        else:
            source = inside(
                folder / "generated", args["artifact"].removeprefix("generated/")
            )
        dest = folder / "generated" / ("frame-" + uuid.uuid4().hex + ".jpg")
        dest.parent.mkdir(exist_ok=True)
        t = float(args.get("time", 0))
        if not math.isfinite(t) or t < 0:
            raise ValueError("时间无效")
        if source.suffix.lower() in (".jpg", ".jpeg", ".png"):
            with Image.open(source) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((1280, 1280))
                im.save(dest)
        else:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-ss",
                    str(t),
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=1280:720:force_original_aspect_ratio=decrease",
                    str(dest),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return {"image": str(dest), "artifact": str(dest.relative_to(folder))}
    if action == "render_preview":
        from .worker import render

        dest = job / ("preview-" + uuid.uuid4().hex)
        dest.mkdir()
        write_json(dest / "input-edl.json", edl)
        render(folder, dest, dict(request, mode="preview"))
        result = read_json(dest / "result.json")
        write_json(job / "agent-preview.json", result)
        write_json(job / "preview-edl.json", edl)
        return {
            "preview_ready": True,
            "revision": result["revision"],
            "width": result["width"],
            "height": result["height"],
            "decode_ok": True,
            "audio": result.get("audio"),
        }
    if action == "history":
        return {
            "revisions": [
                {"revision": p.stem, "modified": p.stat().st_mtime}
                for p in sorted(
                    (folder / "history").glob("*.json"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
            ]
        }
    if action == "restore":
        rev = args["revision"]
        if (
            not isinstance(rev, str)
            or len(rev) != 64
            or any(c not in "0123456789abcdef" for c in rev)
        ):
            raise ValueError("无效版本号")
        new = read_json(inside(folder / "history", rev + ".json"))
        return {
            "revision": trusted_save(folder, edl, new),
            "restored": True,
            "needs_preview": True,
        }
    raise ValueError("未知工具动作，请调用 help")


def main():
    folder, job = map(lambda p: Path(p).resolve(), sys.argv[1:3])
    action = sys.argv[3]
    try:
        import fcntl

        with (
            (folder / "agent-tool.lock").open("a") as lock,
            (job / "tools.log").open("a") as log,
            contextlib.redirect_stdout(log),
        ):
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = execute(folder, job, action, json.loads(sys.argv[4]))
        if isinstance(result, dict) and result.get("artifact"):
            path = job / "artifacts.json"
            items = read_json(path) if path.exists() else []
            items.append(
                {
                    "path": result["artifact"],
                    "action": action,
                    "applied": result.get("applied", False),
                }
            )
            write_json(path, items)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        message = str(exc)
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            if os.environ.get(key):
                message = message.replace(os.environ[key], "[REDACTED]")
        print(json.dumps({"error": message}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
