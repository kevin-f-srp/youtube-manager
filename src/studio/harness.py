"""Pinned Harness integration. No model-facing shell or filesystem tools."""
import base64
import json
import os
from pathlib import Path

PROVIDERS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def patch_for(provider, model, base_url=None):
    route = {"apiKeyEnv": PROVIDERS[provider], "models": [{"id": model}],
             "retryPolicy": {"mode": "normal", "maxRetries": 1}}
    if base_url:
        route["baseURL"] = base_url
    rows = [{"id": key, "disabled": True} for key in (
        "persistent-bash", "persistent-pwsh", "tool-fs", "tool-working-directory",
        "session-log-deepseek", "plugin-package-inventory-deepseek", "llm-deepseek")]
    rows.append({"insert": [{"id": "studio-models", "name": "@deepseek-ai/dsh-llm-pi-ai",
                             "config": {"providers": {provider: route}}}]})
    return rows  # JSON is also valid YAML; no interpolation of user strings.


def propose(home, provider, model, prompt, images=(), base_url=None):
    from deepseek_harness import DeepSeekHarness
    if provider not in PROVIDERS or not model.strip():
        raise ValueError("请选择 GPT 或 Claude 并填写模型名称")
    if not os.environ.get(PROVIDERS[provider]):
        raise ValueError(f"服务进程未配置 {PROVIDERS[provider]}；请在本机设置后重新启动")
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    patch = home / "studio.patch.json"
    patch.write_text(json.dumps(patch_for(provider, model, base_url)), encoding="utf-8")
    content = [{"type": "text", "text": prompt}]
    for name, path in images:
        content.extend([{"type": "text", "text": f"素材：{name}"},
                        {"type": "image", "mimeType": "image/jpeg", "name": name,
                         "data": base64.b64encode(Path(path).read_bytes()).decode()}])
    with DeepSeekHarness(provider=provider, model=model, max_tokens=12000,
                         cwd=str(home), runtime_cwd=str(home), dsh_home=str(home / "runtime"),
                         profile="sdk-minimal", patches=(str(patch),)) as harness:
        result = harness.run(content)
    if result.finish_reason != "completed":
        raise RuntimeError(f"模型任务未完成：{result.finish_reason}")
    answer = result.final_response.strip()
    if answer.startswith("```"):
        answer = answer.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(answer)
