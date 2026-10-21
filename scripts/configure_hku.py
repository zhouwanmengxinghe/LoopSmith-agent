from __future__ import annotations

import argparse
from pathlib import Path


# 更新非敏感 HKU 配置，保留已有密钥且不输出 .env 内容
def main() -> None:
    parser = argparse.ArgumentParser(description="Configure HKU without displaying secrets")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    settings = {
        "LOOPSMITH_LLM_PROVIDER": "hku",
        "LOOPSMITH_LLM_DEFAULT_MODEL": "DeepSeek-V4-Flash",
        "LOOPSMITH_LLM_BASE_URL": "https://api.hku.hk/foundry/student/models",
        "LOOPSMITH_LLM_MAX_OUTPUT_TOKENS": "4096",
        "LOOPSMITH_LLM_TIMEOUT_S": "120",
        "LOOPSMITH_LLM_CONTEXT_WINDOW": "0",
    }
    path = args.env_file
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key not in settings:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in settings.items())
    has_key = any(line.strip().startswith("HKU_API_KEY=") for line in output)
    if not has_key:
        output.append("HKU_API_KEY=")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    print("HKU non-secret settings saved. Set HKU_API_KEY locally before live use.")


if __name__ == "__main__":
    main()
