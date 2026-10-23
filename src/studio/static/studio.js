"use strict";
const $ = (s) => document.querySelector(s);
let config,
  current,
  selectedShot = null,
  dirty = false,
  jobSignature = "",
  projectId = "",
  showingSource = false;
let sourceAsset = null,
  playbackJob = null,
  reviewAnchor = null;
let sending = false;
let latestAgentJobs = [];
const chosen = new Set();
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  setTimeout(() => ($("#toast").hidden = true), 7000);
}
async function api(path, method = "GET", body) {
  const r = await fetch("/api" + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Studio-Token": config?.token || "",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let d;
  try {
    d = await r.json();
  } catch {
    throw Error(`请求失败 (${r.status})`);
  }
  if (!r.ok) throw Error(d.error || `请求失败 (${r.status})`);
  return d;
}
const base = () => `/projects/${projectId}`;
function mark() {
  dirty = true;
  renderRanges();
  $("#dirty").textContent = "有未保存修改";
}
function duration(s) {
  let t = s.out - s.in;
  if (s.skip_on !== false)
    for (const [a, b] of s.skip || [])
      t -= Math.max(0, Math.min(b, s.out) - Math.max(a, s.in));
  return Math.max(0, t) / (s.speed || 1);
}
function timeAt(id) {
  let t = 0;
  for (const s of current.edl.shots) {
    if (s.id === id) return t;
    if (s.enabled !== false) t += duration(s);
  }
  return t;
}
function credentialStatus() {
  const p = config.providers.find((p) => p.id === $("#provider").value);
  $("#credentials").textContent =
    `${p.env}：${p.configured ? "已填写（尚未验证可用性）" : "尚未配置，可在下方输入密钥或设置服务环境变量"}。`;
}
async function refreshProjects(select) {
  const ps = await api("/projects");
  $("#projects").replaceChildren(el("option", "选择项目"));
  $("#projects").firstChild.value = "";
  for (const p of ps) {
    const o = el("option", p.name);
    o.value = p.id;
    $("#projects").append(o);
  }
  if (select) {
    $("#projects").value = select;
    await loadProject(select);
  }
}
async function loadProject(id) {
  projectId = id;
  if (id) localStorage.setItem("studio-project", id);
  chosen.clear();
  latestAgentJobs = [];
  $("#send-error").hidden = true;
  updateSendStatus();
  clearAnchor();
  selectedShot = null;
  dirty = false;
  jobSignature = "";
  if (!id) return;
  current = await api(base());
  $("#project-name").textContent = current.project.name;
  $("#version").textContent = "版本 " + current.revision.slice(0, 8);
  $("#dirty").textContent = "";
  $("#context").textContent = "发送时自动附带播放器当前画面与时间";
  renderAssets();
  renderShots();
  await showPreview();
  await poll();
}
async function showPreview() {
  if (!current) return;
  const pid = projectId;
  const playback = await api(base() + "/playback");
  if (pid !== projectId) return;
  $("#preview").onloadedmetadata = null;
  showingSource = false;
  sourceAsset = null;
  playbackJob = playback.job_id;
  $("#preview").poster = "";
  $("#preview").src = playback.url;
  $("#preview-label").textContent = "最近一次成片预览（修改后需重新生成）";
  renderRanges();
}
function kept(s) {
  let spans = [[s.in, s.out]];
  if (s.skip_on !== false)
    for (const [left, right] of s.skip || []) {
      spans = spans.flatMap(([a, b]) =>
        [
          [a, Math.min(b, left)],
          [Math.max(a, right), b],
        ].filter(([x, y]) => y > x),
      );
    }
  return spans;
}
function renderRanges() {
  const root = $("#used-ranges");
  root.replaceChildren();
  if (!showingSource || !sourceAsset || !current) return;
  root.append(
    el("p", "当前剪辑方案选用的时间段（点击跳转；含未保存修改）", "hint"),
  );
  const bar = el("div", undefined, "range-track");
  root.append(bar);
  const selected = current.edl.shots.find((s) => s.id === selectedShot);
  if (
    sourceAsset.kind === "video" &&
    selected &&
    shotAsset(selected)?.id === sourceAsset.id
  ) {
    root.append(
      el(
        "p",
        `正在对照镜头 ${selected.id}；可播放整段原片，再调整取用范围。`,
        "hint",
      ),
    );
    for (const [key, label] of [
      ["in", "将当前播放位置设为起点"],
      ["out", "将当前播放位置设为终点"],
    ]) {
      const button = el("button", label);
      button.onclick = () => {
        const time = Number($("#preview").currentTime.toFixed(3));
        if (!Number.isFinite($("#preview").duration))
          return toast("请等待原片加载完成");
        if (
          (key === "in" && time >= selected.out) ||
          (key === "out" && time <= selected.in)
        )
          return toast("起点必须早于终点，请先调整另一端");
        selected[key] = time;
        mark();
        renderShots();
        toast("取用范围已调整；保存后生成预览即可查看效果");
      };
      root.append(button);
    }
  }
  let count = 0;
  for (const shot of current.edl.shots) {
    if (
      shot.enabled === false ||
      ![sourceAsset.name, sourceAsset.name.replace(/\.[^.]+$/, "")].includes(
        shot.clip,
      )
    )
      continue;
    for (const [a, b] of kept(shot)) {
      count++;
      const label = `${shot.id} · ${a.toFixed(1)}–${b.toFixed(1)} 秒`;
      const jump = () => {
        $("#preview").currentTime = a;
        $("#preview").pause();
      };
      const button = el("button", label);
      button.onclick = jump;
      root.append(button);
      if (sourceAsset.duration) {
        const block = el("button", undefined, "range-block");
        block.style.left = `${(100 * a) / sourceAsset.duration}%`;
        block.style.width = `${(100 * (b - a)) / sourceAsset.duration}%`;
        block.title = label;
        block.setAttribute("aria-label", label);
        block.onclick = jump;
        bar.append(block);
      }
    }
  }
  bar.hidden = !sourceAsset.duration;
  if (!count) root.append(el("p", "这份素材尚未被选入当前剪辑方案。", "hint"));
}
function clearAnchor() {
  reviewAnchor = null;
  $("#context").textContent = "发送时自动附带播放器当前画面与时间";
  $("#context-frame").hidden = true;
  $("#clear-context").hidden = true;
}
function anchorFrame(pause = true) {
  if (!current) throw Error("请先选择项目");
  const video = $("#preview");
  const photo = showingSource && sourceAsset?.kind === "photo";
  if (!photo && (video.readyState < 2 || video.seeking))
    throw Error("请等待画面加载后再定位");
  if (pause) video.pause();
  reviewAnchor = showingSource
    ? {
        kind: "source",
        asset_id: sourceAsset.id,
        time: photo ? 0 : video.currentTime,
      }
    : { kind: "cut", job_id: playbackJob, time: video.currentTime };
  $("#context").textContent =
    `已定位：${showingSource ? "原素材 · " + sourceAsset.name : "成片"} · ${reviewAnchor.time.toFixed(2)} 秒`;
  const image = $("#context-frame");
  if (photo) image.src = video.poster;
  else {
    const canvas = document.createElement("canvas");
    canvas.width = 480;
    canvas.height = (480 * video.videoHeight) / video.videoWidth;
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    image.src = canvas.toDataURL("image/jpeg", 0.8);
  }
  image.hidden = false;
  $("#clear-context").hidden = false;
  if (pause) $("#message").focus();
}
function openSource(a, shot = null) {
  showingSource = true;
  sourceAsset = a;
  playbackJob = null;
  if (shot) selectedShot = shot.id;
  const player = $("#preview");
  player.pause();
  player.onloadedmetadata = null;
  $("#welcome").hidden = true;
  if (a.kind === "video") {
    player.poster = "";
    player.onloadedmetadata = () => {
      player.currentTime = shot ? (kept(shot)[0]?.[0] ?? shot.in) : 0;
      player.onloadedmetadata = null;
    };
    player.src = "/api" + base() + `/assets/${a.id}/original`;
  } else {
    player.removeAttribute("src");
    player.load();
    player.poster = "/api" + base() + `/assets/${a.id}/original`;
  }
  $("#preview-label").textContent = "原素材 · " + a.name;
  clearAnchor();
  renderRanges();
  renderShots();
  player.scrollIntoView({ behavior: "smooth", block: "center" });
}
function shotAsset(shot) {
  return current.assets.find((a) =>
    [a.name, a.name.replace(/\.[^.]+$/, "")].includes(shot.clip),
  );
}
function renderAssets() {
  $("#asset-count").textContent = current.assets.length;
  const root = $("#assets");
  root.replaceChildren();
  for (const a of current.assets) {
    const box = el("div", undefined, "asset");
    const check = el("input");
    check.type = "checkbox";
    check.checked = chosen.has(a.id);
    check.setAttribute("aria-label", "选择 " + a.name);
    check.onchange = () => {
      if (check.checked) chosen.add(a.id);
      else chosen.delete(a.id);
    };
    const img = el("img");
    img.alt = a.name;
    img.loading = "lazy";
    if (a.thumbnail) img.src = "/api" + base() + `/assets/${a.id}/thumbnail`;
    img.onclick = () => openSource(a);
    const add = el("button", "＋");
    add.title = "加入镜头列表";
    add.onclick = () => {
      const s = {
        id: "s" + crypto.randomUUID().replaceAll("-", "").slice(0, 12),
        clip: a.kind === "photo" ? a.name : a.name.replace(/\.[^.]+$/, ""),
        in: 0,
        out: a.kind === "photo" ? 3 : Math.min(6, a.duration || 6),
        enabled: true,
        audio: a.kind === "photo" ? "mute" : "ambient",
        note: a.name,
      };
      current.edl.shots.push(s);
      mark();
      renderShots();
    };
    box.append(
      img,
      check,
      add,
      el("div", `${a.kind === "photo" ? "照片" : "视频"} · ${a.name}`, "meta"),
    );
    root.append(box);
  }
}
function renderShots() {
  const root = $("#shots");
  root.replaceChildren();
  if (!current.edl.shots.length) {
    root.append(
      el("p", "点击素材上的 ＋ 添加镜头，或让剪辑助手生成初稿。", "empty"),
    );
    return;
  }
  current.edl.shots.forEach((s, i) => {
    const row = el(
      "div",
      undefined,
      "shot" + (selectedShot === s.id ? " active" : ""),
    );
    const head = el("div", undefined, "shot-head");
    const on = el("input");
    on.type = "checkbox";
    on.checked = s.enabled !== false;
    on.setAttribute("aria-label", "启用镜头 " + s.id);
    on.onclick = (e) => e.stopPropagation();
    on.onchange = () => {
      s.enabled = on.checked;
      mark();
    };
    head.append(
      on,
      el(
        "span",
        `${s.id} · ${timeAt(s.id).toFixed(1)}s · ${s.note || s.clip || "标题卡"}`,
      ),
    );
    head.onclick = async () => {
      selectedShot = s.id;

      if (showingSource) await showPreview();
      $("#preview").currentTime = timeAt(s.id);
      renderShots();
    };
    for (const [label, delta] of [
      ["↑", -1],
      ["↓", 1],
    ]) {
      const b = el("button", label);
      b.onclick = (e) => {
        e.stopPropagation();
        const j = i + delta;
        if (j < 0 || j >= current.edl.shots.length) return;
        [current.edl.shots[i], current.edl.shots[j]] = [
          current.edl.shots[j],
          current.edl.shots[i],
        ];
        mark();
        renderShots();
      };
      head.append(b);
    }
    row.append(head);
    const asset = shotAsset(s);
    if (asset) {
      const source = el("div", undefined, "shot-source");
      const open = el("button", undefined, "source-thumbnail");
      open.setAttribute("aria-label", `查看镜头 ${s.id} 的原素材`);
      if (asset.thumbnail) {
        const img = el("img");
        img.src = "/api" + base() + `/assets/${asset.id}/thumbnail`;
        img.alt = asset.name;
        img.loading = "lazy";
        open.append(img);
      } else open.textContent = "预览";
      open.onclick = () => openSource(asset, s);
      const info = el("div");
      info.append(el("div", asset.name));
      info.append(
        el(
          "p",
          asset.kind === "video"
            ? `原片选用：${kept(s)
                .map(([a, b]) => `${a.toFixed(1)}–${b.toFixed(1)} 秒`)
                .join(
                  "、",
                )} · 原片共 ${Number(asset.duration || 0).toFixed(1)} 秒`
            : `照片 · 成片展示 ${duration(s).toFixed(1)} 秒`,
          "hint",
        ),
      );
      const view = el(
        "button",
        asset.kind === "video" ? "查看原片 / 调整选段" : "查看原图",
      );
      view.onclick = () => openSource(asset, s);
      const original = el("a", "单独打开原素材");
      original.href = "/api" + base() + `/assets/${asset.id}/original`;
      original.target = "_blank";
      original.rel = "noopener";
      info.append(view, original);
      source.append(open, info);
      row.append(source);
    }
    if (selectedShot === s.id) {
      const fields = el("div", undefined, "shot-fields");
      for (const [key, label] of [
        ["in", "原片起点 / 秒"],
        ["out", "原片终点 / 秒"],
        ["speed", "播放倍速"],
      ]) {
        const l = el("label", label),
          input = el("input");
        input.type = "number";
        input.step = ".1";
        input.value = s[key] ?? 1;
        input.onchange = () => {
          s[key] = Number(input.value);
          mark();
        };
        l.append(input);
        fields.append(l);
      }
      row.append(fields);
      const l = el("label", "画幅"),
        sel = el("select");
      for (const [v, label] of [
        ["auto", "自动"],
        ["cover", "填满画面"],
        ["blur", "完整画幅 + 模糊背景"],
      ]) {
        const op = el("option", label);
        op.value = v;
        sel.append(op);
      }
      sel.value = typeof s.fit === "string" ? s.fit : "auto";
      sel.onchange = () => {
        s.fit = sel.value;
        mark();
      };
      l.append(sel);
      row.append(l);
      const details = el("details"),
        summary = el("summary", "字幕（原素材时间，JSON）"),
        area = el("textarea");
      area.rows = 5;
      area.value = JSON.stringify(s.subs || [], null, 2);
      area.onchange = () => {
        try {
          s.subs = JSON.parse(area.value);
          mark();
        } catch {
          toast("字幕 JSON 格式无效，请修正后保存");
        }
      };
      details.append(summary, area);
      row.append(details);
    }
    root.append(row);
  });
}
async function save() {
  if (!current) throw Error("请先选择项目");
  const d = await api(base() + "/edl", "PUT", {
    revision: current.revision,
    edl: current.edl,
  });
  current.revision = d.revision;
  dirty = false;
  $("#dirty").textContent = "已保存";
  $("#version").textContent = "版本 " + d.revision.slice(0, 8);
}
function updateSendStatus() {
  if (sending || !$("#send-error").hidden) return;
  const latest = latestAgentJobs[0];
  const active = latestAgentJobs.find(
    (j) => j.state === "running" || j.state === "queued",
  );
  const draft = $("#message").value.trim();
  let text;
  if (active) {
    text =
      active.state === "queued" ? "消息已收到，排队等待处理" : "Agent 正在处理";
    text += `：「${active.message.slice(0, 50)}」`;
    if (active.progress?.step) text += " · " + active.progress.step;
    if (draft) text = "输入框中的草稿尚未发送。" + text;
  } else if (draft) text = "草稿尚未发送";
  else if (latest) {
    const states = {
      completed: "已回复",
      failed: "处理失败",
      cancelled: "已取消",
      interrupted: "处理已中断",
    };
    text = `最近一条${states[latest.state] || latest.state}：「${latest.message.slice(0, 50)}」`;
    if (latest.error) text += " · " + (latest.error.error || latest.error);
  } else text = "尚未发送消息";
  $("#send-status").textContent = text;
}
async function start(payload, accepted) {
  if (!current) throw Error("请先导入或选择项目");
  if (dirty) await save();
  const job = await api(base() + "/jobs", "POST", payload);
  if (accepted) accepted(job);
  jobSignature = "";
  // A polling failure must never turn an accepted message into a false send failure.
  await poll().catch(() => {
    $("#agent-state").textContent = "状态暂时无法刷新";
  });
  return job;
}
async function poll() {
  if (!projectId) return;
  const id = projectId,
    js = await api(base() + "/jobs");
  if (id !== projectId) return;
  latestAgentJobs = js.filter((j) => j.kind === "agent");
  updateSendStatus();
  const status = await api(base() + "/agent");
  if (id !== projectId) return;
  $("#agent-state").textContent =
    status.state === "working"
      ? `Agent 正在工作${status.queued ? " · " + status.queued + " 条待处理" : ""}`
      : "Agent 在线 · 可发送消息";
  const freshProject = await api(base());
  if (id !== projectId) return;
  if (!dirty && freshProject.revision !== current.revision) {
    current = freshProject;
    $("#version").textContent = "剪辑方案版本 " + current.revision.slice(0, 8);
    renderShots();
    renderRanges();
  }
  const sig = JSON.stringify(js);
  if (sig === jobSignature) return;
  jobSignature = sig;
  $("#task-count").textContent = js.length;
  const root = $("#jobs");
  root.replaceChildren();
  const chat = $("#chat");
  const previousScroll = chat.scrollTop;
  const followChat =
    chat.scrollHeight - chat.scrollTop - chat.clientHeight < 40;
  chat.replaceChildren();
  for (const j of [...js].reverse()) {
    if (j.message) {
      const prefix = j.context
        ? `[${j.context.kind === "source" ? "原素材 · " + j.context.name : "成片"} ${j.context.time.toFixed(2)} 秒]\n`
        : "";
      chat.append(el("div", prefix + j.message, "bubble user"));
    }
    if (j.result?.answer) chat.append(el("div", j.result.answer, "bubble"));
    if (j.result?.proposal)
      chat.append(el("div", j.result.proposal.summary, "bubble"));
  }
  chat.scrollTop = followChat ? chat.scrollHeight : previousScroll;
  for (const j of js) {
    const box = el("div", undefined, "job"),
      states = {
        running: "处理中",
        queued: "排队中",
        completed: "完成",
        failed: "失败",
        cancelled: "已取消",
        interrupted: "已中断",
      };
    box.append(
      el(
        "strong",
        `${{ scan: "素材整理", agent: "助手回复", render: "视频导出" }[j.kind]} · ${states[j.state] || j.state}`,
      ),
    );
    if (j.progress && j.kind !== "agent") {
      const p = el("progress");
      p.max = 1;
      p.value = j.progress.progress || 0;
      box.append(el("p", j.progress.step || "处理中"), p);
    }
    if (j.error)
      box.append(
        el("p", typeof j.error === "string" ? j.error : j.error.error),
      );
    if (["running", "queued"].includes(j.state)) {
      const b = el("button", "取消");
      b.onclick = () =>
        safe(async () => {
          await api(base() + `/jobs/${j.id}/cancel`, "POST", {});
          await poll();
        });
      box.append(b);
    }
    if (j.result?.proposal && !j.result.changes?.length) {
      box.append(el("p", "仅回答问题，没有修改剪辑方案，无需应用。"));
    }
    if (j.result?.proposal && j.result?.changes?.length) {
      const details = el("details");
      details.append(
        el("summary", `查看 ${j.result.changes.length} 项实际修改`),
      );
      const labels = {
        in: "原片起点",
        out: "原片终点",
        speed: "播放倍速",
        enabled: "启用",
        fit: "画幅",
        zoom: "缩放",
        subs: "字幕",
        note: "镜头说明",
      };
      for (const c of j.result.changes) {
        details.append(
          el("strong", `${c.shot_id} · ${labels[c.field] || c.field}`),
        );
        details.append(
          el(
            "pre",
            `修改前：${JSON.stringify(c.before, null, 2)}\n修改后：${JSON.stringify(c.after, null, 2)}`,
          ),
        );
      }
      box.append(details);
      const b = el("button", "应用建议");
      b.disabled = j.result.base_revision !== current.revision || dirty;
      b.onclick = () =>
        safe(async () => {
          await api(base() + `/jobs/${j.id}/apply`, "POST", {});
          await loadProject(projectId);
          toast("建议已应用，可以生成预览");
        });
      box.append(b);
    }
    if (j.result?.answer)
      box.append(el("p", j.result.applied ? "修改已保存到项目副本" : "已回复"));
    if (j.kind === "agent" || j.events?.length) {
      const details = el("div");
      details.className = "execution-log";
      details.append(el("strong", "执行过程"));
      for (const event of j.events || [])
        if (event.type === "tool/call" || event.type === "tool/result")
          details.append(
            el(
              "p",
              `${event.type === "tool/call" ? "调用" : event.error ? "失败" : "完成"} ${event.label || event.action || event.tool || "剪辑工具"}`,
            ),
          );
      if (j.kind === "agent") {
        const status = {
          queued: "等待前一个任务完成",
          running: j.progress?.step || "正在连接助手，等待执行更新…",
          completed: "处理已完成",
          failed: "处理失败，原因见上方",
          cancelled: "任务已取消",
          interrupted: "任务已中断",
        };
        details.append(el("p", status[j.state] || j.state));
      }
      box.append(details);
    }
    for (const artifact of j.artifacts || []) {
      const a = el("a", artifact.applied ? "查看已生成的素材" : "查看画面预览");
      a.href =
        "/api" +
        base() +
        "/artifacts/" +
        artifact.path.replace(/^generated\//, "");
      a.target = "_blank";
      a.rel = "noopener";
      box.append(a);
    }
    if (j.state === "completed" && j.result?.output) {
      box.append(
        el(
          "p",
          `${j.result.width} × ${j.result.height} · ${Number(j.result.duration).toFixed(1)} 秒 · 完整解码通过`,
        ),
      );
      const a = el("a", "下载成片");
      a.href = "/api" + base() + `/jobs/${j.id}/output?download=1`;
      box.append(a);
      const b = el("button", "播放");
      b.onclick = () => {
        showingSource = false;
        sourceAsset = null;
        playbackJob = j.id;
        renderRanges();
        $("#preview").poster = "";
        $("#preview").src = "/api" + base() + `/jobs/${j.id}/output`;
        $("#preview-label").textContent =
          "导出版本 " + j.result.revision.slice(0, 8);
      };
      box.append(b);
    }
    root.append(box);
  }
  if (
    js.some((j) => j.kind === "scan" && j.state === "completed") &&
    current.assets.some((a) => !a.thumbnail)
  ) {
    const fresh = await api(base());
    current.assets = fresh.assets;
    renderAssets();
  }
}
async function safe(fn) {
  try {
    await fn();
  } catch (e) {
    toast(e.message);
  }
}
$("#import-open").onclick = () => $("#import-dialog").showModal();
$("#import-cancel").onclick = () => $("#import-dialog").close();
$("#import-form").onsubmit = (e) => {
  e.preventDefault();
  safe(async () => {
    const d = await api("/projects", "POST", { path: $("#source-path").value });
    $("#import-dialog").close();
    await refreshProjects(d.id);
  });
};
$("#projects").onchange = () =>
  safe(async () => {
    if (dirty) {
      $("#projects").value = projectId;
      throw Error("请先保存当前修改再切换项目");
    }
    await loadProject($("#projects").value);
  });
$("#save").onclick = () => safe(save);
$("#scan").onclick = () => safe(() => start({ kind: "scan" }));
$("#render-preview").onclick = () =>
  safe(() => start({ kind: "render", mode: "preview" }));
$("#render-final").onclick = () =>
  safe(() => start({ kind: "render", mode: "final" }));
$("#show-preview").onclick = () => safe(showPreview);
$("#anchor-frame").onclick = () =>
  safe(() => {
    $("#attach-frame").checked = true;
    anchorFrame();
  });
$("#clear-context").onclick = clearAnchor;
$("#base-url").oninput = () => {
  localStorage.setItem(
    "studio-base-url-" + $("#provider").value,
    $("#base-url").value.trim(),
  );
};
$("#provider").onchange = () => {
  credentialStatus();
  localStorage.setItem("studio-provider", $("#provider").value);
  $("#model").value =
    localStorage.getItem("studio-model-" + $("#provider").value) || "";
  $("#base-url").value =
    localStorage.getItem("studio-base-url-" + $("#provider").value) || "";
};
$("#message").oninput = updateSendStatus;
$("#send").onclick = async () => {
  if (sending) return;
  sending = true;
  $("#send").disabled = true;
  $("#send").textContent = "正在提交…";
  $("#send-error").hidden = true;
  $("#send-status").textContent = "正在提交消息，等待服务确认…";
  try {
    const message = $("#message").value.trim();
    if (!message) throw Error("请输入消息");
    config = await api("/config");
    credentialStatus();
    const provider = $("#provider").value;
    if (!config.providers.find((p) => p.id === provider)?.configured) {
      $("#api-key").closest("details").open = true;
      throw Error(
        "消息未发送：当前服务没有 API 密钥（服务重启后需要重新填写）。请在「连接配置」填写密钥并点击「配置密钥」，再发送。代理地址仍会保留。",
      );
    }
    if ($("#attach-frame").checked) anchorFrame(false);
    else reviewAnchor = null;
    localStorage.setItem("studio-model-" + provider, $("#model").value);
    await start(
      {
        kind: "agent",
        provider,
        model: $("#model").value.trim(),
        base_url: $("#base-url").value.trim() || null,
        message,
        context: reviewAnchor,
        asset_ids: [...chosen],
      },
      (job) => {
        latestAgentJobs = [job, ...latestAgentJobs];
        if ($("#message").value.trim() === message) $("#message").value = "";
        clearAnchor();
        $("#send-status").textContent =
          job.state === "queued"
            ? "消息已收到，排队等待处理"
            : "消息已收到，Agent 正在处理";
      },
    );
  } catch (error) {
    $("#send-status").textContent = "发送失败，草稿已保留";
    $("#send-error").textContent = error.message;
    $("#send-error").hidden = false;
  } finally {
    sending = false;
    $("#send").disabled = false;
    $("#send").textContent = "发送";
    updateSendStatus();
  }
};
$("#preview").onloadedmetadata = () => {
  $("#welcome").hidden = true;
  $("#preview").poster = "";
};
$("#preview").onerror = () => {
  if (!showingSource) $("#welcome").hidden = false;
};
window.addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
safe(async () => {
  config = await api("/config");
  $("#connection").textContent = config.harness_installed
    ? "本机服务已连接 · Harness 已安装"
    : "本机服务已连接 · 需安装 Harness";
  $("#provider").value = localStorage.getItem("studio-provider") || "openai";
  $("#model").value =
    localStorage.getItem("studio-model-" + $("#provider").value) || "";
  $("#base-url").value =
    localStorage.getItem("studio-base-url-" + $("#provider").value) || "";
  credentialStatus();
  await refreshProjects();
  const last = localStorage.getItem("studio-project");
  if (last && [...$("#projects").options].some((o) => o.value === last)) {
    $("#projects").value = last;
    await loadProject(last);
  }
  setInterval(
    () =>
      poll().catch(() => {
        $("#agent-state").textContent = "Agent 服务未连接";
      }),
    2000,
  );
});

$("#new-session").onclick = () =>
  safe(async () => {
    if (!current) throw Error("请先选择项目");
    await api(base() + "/agent/reset", "POST", {});
    toast("已新建对话，工程和旧任务记录保留");
  });

$("#set-key").onclick = () =>
  safe(async () => {
    await api("/model-credentials", "POST", {
      provider: $("#provider").value,
      key: $("#api-key").value,
    });
    $("#api-key").value = "";
    config = await api("/config");
    credentialStatus();
    $("#send-error").hidden = true;
    updateSendStatus();
    toast("密钥已配置，仅保存在本次服务内存中");
  });
