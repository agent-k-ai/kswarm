"""
LLM客户端封装
统一使用OpenAI格式调用
"""

import json
import re
from dataclasses import dataclass
from typing import Optional, Dict, Any, List
from openai import OpenAI

from ..config import Config


def _salvage_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """从可能夹杂推理正文的文本中提取最后一个可解析的顶层 JSON 对象。

    面向推理模型：当 content 形如 "<思考...> {\"scalar_value\": 0.36, ...}" 时，
    直接 json.loads 会失败。此函数用花括号配对（正确跳过字符串/转义）扫描所有
    平衡的 ``{...}`` 片段，返回最后一个能 json.loads 成功的对象；找不到则返回 None。
    仅在直接解析失败时调用，不影响正常路径。
    """

    if not text:
        return None
    spans: List[str] = []
    depth = 0
    start: Optional[int] = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append(text[start : i + 1])
                start = None
    for candidate in reversed(spans):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


@dataclass(frozen=True)
class LLMChatResult:
    content: str
    completion_tokens: Optional[int]
    prompt_tokens: Optional[int]
    total_tokens: Optional[int]


@dataclass(frozen=True)
class LLMJsonResult:
    payload: Dict[str, Any]
    completion_tokens: Optional[int]
    prompt_tokens: Optional[int]
    total_tokens: Optional[int]
    raw_content: str


class LLMClient:
    """LLM客户端"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or Config.LLM_API_KEY
        self.base_url = base_url or Config.LLM_BASE_URL
        self.model = model or Config.LLM_MODEL_NAME
        
        if not self.api_key:
            if self.base_url and "api.openai.com" not in self.base_url:
                self.api_key = "local-llm"
            else:
                raise ValueError("LLM_API_KEY 未配置")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None,
        seed: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        发送聊天请求
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            response_format: 响应格式（如JSON模式）
            
        Returns:
            模型响应文本
        """
        return self.chat_with_metadata(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            seed=seed,
            extra_body=extra_body,
        ).content

    def chat_with_metadata(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: Optional[Dict] = None,
        seed: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> LLMChatResult:
        """发送聊天请求并返回文本及 token 用量。"""

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        if response_format:
            kwargs["response_format"] = response_format
        if seed is not None:
            kwargs["seed"] = seed
        if extra_body:
            kwargs["extra_body"] = extra_body
        
        response = self.client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        content = message.content
        if not content:
            # 推理模型（如经 vLLM reasoning-parser 部署的 Qwen3）在未产出正文时，
            # 会把答案放进独立的 reasoning 字段。此处回退读取，避免在 content 为 None
            # 时崩溃；若两者皆空则抛出清晰错误而非 TypeError。
            content = getattr(message, "reasoning", None) or getattr(message, "reasoning_content", None)
        if content is None:
            raise ValueError("LLM返回空内容（content 与 reasoning 字段均为空），可能是 max_tokens 过小导致截断")
        # 部分模型（如MiniMax M2.5）会在content中包含<think>思考内容，需要移除
        content = re.sub(r'<think>[\s\S]*?</think>', '', content).strip()
        usage = response.usage
        return LLMChatResult(
            content=content,
            completion_tokens=usage.completion_tokens if usage else None,
            prompt_tokens=usage.prompt_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
        )
    
    def chat_json(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        seed: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        发送聊天请求并返回JSON
        
        Args:
            messages: 消息列表
            temperature: 温度参数
            max_tokens: 最大token数
            
        Returns:
            解析后的JSON对象
        """
        return self.chat_json_with_metadata(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            seed=seed,
            extra_body=extra_body,
        ).payload

    def chat_json_with_metadata(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        seed: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
    ) -> LLMJsonResult:
        """发送聊天请求并返回解析后的 JSON 和 token 用量。"""

        response = self.chat_with_metadata(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            seed=seed,
            extra_body=extra_body,
        )
        # 清理markdown代码块标记
        cleaned_response = response.content.strip()
        cleaned_response = re.sub(r'^```(?:json)?\s*\n?', '', cleaned_response, flags=re.IGNORECASE)
        cleaned_response = re.sub(r'\n?```\s*$', '', cleaned_response)
        cleaned_response = cleaned_response.strip()

        try:
            payload = json.loads(cleaned_response)
        except json.JSONDecodeError:
            # 推理模型（如经 vLLM 部署的 Qwen3）有时会把思考正文与最终 JSON 一起
            # 放进 content，而非分离到 reasoning 字段；此时直接 json.loads 会失败。
            # 回退：从原始 content 中提取最后一个可解析的顶层 JSON 对象；仅在直接
            # 解析失败时触发，成功路径行为不变。
            salvaged = _salvage_json_object(response.content)
            if salvaged is None:
                raise ValueError(f"LLM返回的JSON格式无效: {cleaned_response}")
            payload = salvaged
        return LLMJsonResult(
            payload=payload,
            completion_tokens=response.completion_tokens,
            prompt_tokens=response.prompt_tokens,
            total_tokens=response.total_tokens,
            raw_content=cleaned_response,
        )
