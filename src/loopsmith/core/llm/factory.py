from loopsmith.core.config import LlmConfig
from loopsmith.core.llm.base import LLMProvider
from loopsmith.core.llm.hku import HkuProvider
from loopsmith.core.llm.provider import AnthropicProvider


# 按显式配置创建模型适配器，HKU 不回退到其他供应商
# max_output_tokens / max_stream_retries / timeout_s 只在实验基线等需要固定预算时传入，
# 默认沿用各适配器自身取值，保证应用侧行为不变
def create_provider(
    config: LlmConfig,
    *,
    max_output_tokens: int | None = None,
    max_stream_retries: int | None = None,
    timeout_s: float | None = None,
    sdk_max_retries: int | None = None,
    deepseek_thinking: bool = False,
    deepseek_thinking_effort: str = "low",
) -> LLMProvider:
    if config.provider == "hku":
        if deepseek_thinking:
            raise ValueError("DeepSeek thinking experiment requires the Anthropic provider")
        return HkuProvider(
            config.default_model,
            base_url=config.base_url,
            max_output_tokens=(
                config.max_output_tokens if max_output_tokens is None else max_output_tokens
            ),
            context_window=config.context_window,
            timeout_s=config.timeout_s if timeout_s is None else int(timeout_s),
        )
    extra: dict[str, int] = {}
    if max_output_tokens is not None:
        extra["max_tokens"] = max_output_tokens
    if max_stream_retries is not None:
        extra["max_stream_retries"] = max_stream_retries
    if sdk_max_retries is not None:
        extra["sdk_max_retries"] = sdk_max_retries
    return AnthropicProvider(
        config.default_model,
        base_url=config.base_url,
        deepseek_thinking=deepseek_thinking,
        deepseek_thinking_effort=deepseek_thinking_effort,
        **extra,
    )
