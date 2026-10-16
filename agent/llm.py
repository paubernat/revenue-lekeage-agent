from langchain_anthropic import ChatAnthropic

import config


def get_llm() -> ChatAnthropic:
    return ChatAnthropic(model=config.MODEL, max_tokens=4096)
