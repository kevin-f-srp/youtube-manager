"""Pinned Harness integration. No model-facing shell or filesystem tools."""

import base64
import json
import os
from pathlib import Path

PROVIDERS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def patch_for(provider, model, base_url=None):
    route = {
        "apiKeyEnv": PROVIDERS[provider],
        "models": [{"id": model}],
        "retryPolicy": {"mode": "normal", "maxRetries": 1},
    }
    if base_url:
        route["baseURL"] = base_url
    rows = [
        {"id": key, "disabled": True}
        for key in (
            "persistent-bash",
            "persistent-pwsh",
            "tool-fs",
            "tool-working-directory",
            "session-log-deepseek",
            "plugin-package-inventory-deepseek",
            "llm-deepseek",
        )
    ]
    rows.append(
        {
            "insert": [
                {
                    "id": "studio-models",
                    "name": "@deepseek-ai/dsh-llm-pi-ai",
                    "config": {"providers": {provider: route}},
                }
            ]
        }
    )
    return rows  # JSON is also valid YAML; no interpolation of user strings.


def failure_message(result):
    """Classify provider failures without exposing echoed credentials or response bodies."""
    reason = {}
    for event in reversed(result.events):
        if event.get("type") == "turn/end":
            reason = event.get("data", {}).get("reason", {})
            break
    error = reason.get("error", {})
    message = str(error.get("message", "")) if isinstance(error, dict) else ""
    lower = message.lower()
    if "401" in lower or "invalid_api_key" in lower or "authentication" in lower:
        return "模型服务认证失败（401）：请核对 API 密钥与兼容服务地址是否属于同一服务；已配置不代表验证通过。"
    if "429" in lower or "rate_limit" in lower:
        return "模型服务拒绝请求（429）：请检查账户额度或请求频率。"
    if "model_not_found" in lower or "404" in lower:
        return "模型或接口不存在（404）：请核对模型名称、访问权限及兼容服务地址。"
    if "403" in lower:
        return "模型服务拒绝访问（403）：请检查账户权限与服务限制。"
    if "timeout" in lower or "timed out" in lower:
        return "连接模型服务超时，请检查服务地址与网络后重试。"
    return f"模型任务未完成（{result.finish_reason}）：请检查模型名称、服务地址及服务状态。"


def propose(home, provider, model, prompt, images=(), base_url=None):
    from deepseek_harness import DeepSeekHarness

    if provider not in PROVIDERS or not model.strip():
        raise ValueError("请选择 GPT 或 Claude 并填写模型名称")
    if not os.environ.get(PROVIDERS[provider]):
        raise ValueError(
            f"服务进程未配置 {PROVIDERS[provider]}；请在本机设置后重新启动"
        )
    images = list(images)
    if len(images) > 20:
        raise ValueError("本次请求超过 20 张图片，请减少素材范围后重试")
    home = Path(home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    patch = home / "studio.patch.json"
    patch.write_text(json.dumps(patch_for(provider, model, base_url)), encoding="utf-8")
    content = [{"type": "text", "text": prompt}]
    for name, path in images:
        content.extend(
            [
                {"type": "text", "text": f"素材：{name}"},
                {
                    "type": "image",
                    "mimeType": "image/jpeg",
                    "name": name,
                    "data": base64.b64encode(Path(path).read_bytes()).decode(),
                },
            ]
        )
    with DeepSeekHarness(
        provider=provider,
        model=model,
        max_tokens=12000,
        cwd=str(home),
        runtime_cwd=str(home),
        dsh_home=str(home / "runtime"),
        profile="sdk-minimal",
        patches=(str(patch),),
    ) as harness:
        result = harness.run(content)
    if result.finish_reason != "completed":
        raise RuntimeError(failure_message(result))
    answer = result.final_response.strip()
    if answer.startswith("```"):
        answer = answer.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(answer)
