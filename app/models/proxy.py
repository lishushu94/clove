"""
代理相关数据模型
Proxy-related data models for dynamic proxy pool management.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from urllib.parse import quote

from pydantic import BaseModel, Field


# 代理模式枚举
class ProxyMode(str, Enum):
    """Proxy mode enumeration."""

    DISABLED = "disabled"  # 不使用代理
    FIXED = "fixed"  # 固定代理（单一 URL）
    DYNAMIC = "dynamic"  # 动态代理池（轮换策略）


# 轮换策略枚举
class RotationStrategy(str, Enum):
    """Rotation strategy enumeration."""

    SEQUENTIAL = "sequential"  # 顺序循环
    RANDOM = "random"  # 每次随机选择
    RANDOM_NO_REPEAT = "random_no_repeat"  # 打乱后依次，用完重新打乱
    PER_ACCOUNT = "per_account"  # 同一账户映射到同一代理


# 代理配置模型（用于 config.json）
class ProxySettings(BaseModel):
    """Proxy settings model for configuration."""

    mode: ProxyMode = Field(default=ProxyMode.DISABLED, description="Proxy mode")
    fixed_url: Optional[str] = Field(default=None, description="Fixed proxy URL")
    rotation_strategy: RotationStrategy = Field(
        default=RotationStrategy.SEQUENTIAL, description="Rotation strategy for dynamic mode"
    )
    rotation_interval: int = Field(
        default=300, description="Rotation interval in seconds (for non-per_account strategies)"
    )
    cooldown_duration: int = Field(
        default=1800, description="Cooldown duration in seconds after marking unhealthy"
    )
    fallback_strategy: RotationStrategy = Field(
        default=RotationStrategy.SEQUENTIAL,
        description="Fallback strategy when account_id is not available in per_account mode",
    )


@dataclass
class ProxyInfo:
    """
    代理信息数据类
    Proxy information dataclass for runtime proxy management.
    """

    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    protocol: str = "http"
    cooldown_until: Optional[datetime] = field(default=None)  # 冷却结束时间，None 表示健康

    @property
    def url(self) -> str:
        """Get full proxy URL with authentication."""
        if self.username and self.password:
            # URL encode username and password to handle special characters
            encoded_user = quote(self.username, safe="")
            encoded_pass = quote(self.password, safe="")
            return f"{self.protocol}://{encoded_user}:{encoded_pass}@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def url_safe(self) -> str:
        """Get proxy URL with hidden authentication (for logging)."""
        auth_hint = "[auth]@" if self.username else ""
        return f"{self.protocol}://{auth_hint}{self.host}:{self.port}"

    @property
    def is_available(self) -> bool:
        """Check if proxy is available (healthy or cooldown expired)."""
        if self.cooldown_until is None:
            return True
        if datetime.now() >= self.cooldown_until:
            # Cooldown expired, auto-recover to healthy
            self.cooldown_until = None
            return True
        return False

    @property
    def proxy_id(self) -> str:
        """Get unique proxy identifier."""
        return f"{self.protocol}://{self.host}:{self.port}"

    def mark_unhealthy(self, cooldown_seconds: int) -> None:
        """Mark proxy as unhealthy and set cooldown period."""
        self.cooldown_until = datetime.now().replace(microsecond=0) + timedelta(seconds=cooldown_seconds)

    def __hash__(self) -> int:
        return hash(self.proxy_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProxyInfo):
            return False
        return self.proxy_id == other.proxy_id
