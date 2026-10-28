import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .db import REPO, CliError

REQUIRED = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")


class LlmError(Exception):
    """单个请求失败。不含密钥。"""


def load_env(path=None):
    p = Path(path).expanduser() if path else REPO / ".env"
    if not p.exists():
        raise CliError("bad_input", f"缺少 {p},请复制 .env.example 并填写")
    env = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    empty = [k for k in REQUIRED if not env.get(k)]
    if empty:
        raise CliError("bad_input", f".env 里这些字段为空: {', '.join(empty)}")
    return env


class Llm:
    def __init__(self, env):
        self.url = env["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions"
        self.key = env["OPENAI_API_KEY"]
        self.model = env["OPENAI_MODEL"]
        self.timeout = int(env.get("SUMMARY_TIMEOUT_SECONDS") or 60)
        self.retry_sleep = float(env.get("SUMMARY_RETRY_SLEEP") or 2)
        self.thinking_off = env.get("SUMMARY_DISABLE_THINKING", "1") not in ("0", "false", "no")

    def _scrub(self, text):
        return str(text).replace(self.key, "<key>")

    def chat(self, messages, max_tokens=800, attempts=3):
        body = {"model": self.model, "temperature": 0.3, "max_tokens": max_tokens, "messages": messages}
        if self.thinking_off:
            body["thinking"] = {"type": "disabled"}  # 摘要是简单提炼,关推理更快;默认开推理会吃光 max_tokens 导致空回复
        data = json.dumps(body).encode()
        last = "未知错误"
        for i in range(attempts):
            req = urllib.request.Request(self.url, data=data, method="POST", headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + self.key})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
                content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                if content.strip():
                    return content
                last = "空回复"
            except urllib.error.HTTPError as e:
                detail = self._scrub(e.read().decode("utf-8", "replace")[:200])
                last = f"HTTP {e.code} {detail}"
                if e.code not in (408, 409, 425, 429) and e.code < 500:
                    raise LlmError(last)  # 4xx(鉴权、参数)重试没有意义
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as e:
                last = f"{type(e).__name__} {self._scrub(e)[:150]}"
            if i < attempts - 1:
                time.sleep(self.retry_sleep * (i + 1))
        raise LlmError(last)
