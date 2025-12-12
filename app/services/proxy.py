"""
代理池服务
Proxy pool service for dynamic proxy management.
"""

import asyncio
import contextlib
import hashlib
import random
import re
from collections import deque
from typing import Dict, List, Optional
from urllib.parse import urlparse

from loguru import logger

from app.core.config import settings
from app.core.exceptions import AllProxiesUnavailableError
from app.models.proxy import ProxyInfo, ProxyMode, ProxySettings, RotationStrategy


class ProxyParser:
    """
    代理字符串解析器
    Parser for various proxy string formats.
    """

    # 支持的协议
    SUPPORTED_PROTOCOLS = {"http", "https", "socks5", "socks5h"}

    # URL 格式正则：protocol://[user:pass@]host:port
    URL_PATTERN = re.compile(
        r"^(?P<protocol>https?|socks5h?)://"
        r"(?:(?P<user>[^:@]+):(?P<pass>[^@]+)@)?"
        r"(?P<host>[^:]+):(?P<port>\d+)$"
    )

    # 简化格式正则：host:port 或 host:port:user:pass 或 user:pass:host:port
    SIMPLE_PATTERN = re.compile(r"^[^:/]+:\d+(?::[^:]+:[^:]+)?$")

    @classmethod
    def parse_line(cls, line: str) -> Optional[ProxyInfo]:
        """
        解析单行代理配置
        Parse a single line of proxy configuration.

        Supported formats:
        - URL: http://host:port, http://user:pass@host:port, socks5://host:port
        - Simple: host:port, host:port:user:pass, user:pass:host:port
        """
        line = line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            return None

        # Try URL format first
        if "://" in line:
            return cls._parse_url_format(line)

        # Try simple format
        return cls._parse_simple_format(line)

    @classmethod
    def _parse_url_format(cls, line: str) -> Optional[ProxyInfo]:
        """Parse URL format proxy string."""
        match = cls.URL_PATTERN.match(line)
        if not match:
            logger.warning(f"Invalid proxy URL format: {line[:50]}...")
            return None

        protocol = match.group("protocol")
        host = match.group("host")
        port = int(match.group("port"))
        username = match.group("user")
        password = match.group("pass")

        return ProxyInfo(
            host=host,
            port=port,
            username=username,
            password=password,
            protocol=protocol,
        )

    @classmethod
    def _parse_simple_format(cls, line: str) -> Optional[ProxyInfo]:
        """
        Parse simple format proxy string.

        Formats:
        - host:port (2 parts)
        - host:port:user:pass (4 parts, first two are host:port)
        - user:pass:host:port (4 parts, last two are host:port)
        """
        parts = line.split(":")

        if len(parts) == 2:
            # host:port
            host, port_str = parts
            try:
                port = int(port_str)
                return ProxyInfo(host=host, port=port)
            except ValueError:
                logger.warning(f"Invalid port in proxy: {line}")
                return None

        elif len(parts) == 4:
            # Try host:port:user:pass first (more common)
            host, port_str, user, passwd = parts
            try:
                port = int(port_str)
                return ProxyInfo(host=host, port=port, username=user, password=passwd)
            except ValueError:
                pass

            # Try user:pass:host:port
            user, passwd, host, port_str = parts
            try:
                port = int(port_str)
                return ProxyInfo(host=host, port=port, username=user, password=passwd)
            except ValueError:
                logger.warning(f"Invalid proxy format: {line}")
                return None

        else:
            logger.warning(f"Unsupported proxy format: {line}")
            return None

    @classmethod
    def parse_content(cls, content: str) -> List[ProxyInfo]:
        """
        解析多行代理配置内容
        Parse multi-line proxy configuration content.
        """
        proxies = []
        for line in content.splitlines():
            proxy = cls.parse_line(line)
            if proxy:
                proxies.append(proxy)
        return proxies


class ProxyPool:
    """
    代理池管理服务
    Proxy pool service for dynamic proxy management.

    Features:
    - Multiple rotation strategies (sequential, random, random_no_repeat, per_account)
    - Health check with cooldown recovery
    - Hash-based account-proxy mapping for per_account strategy
    - Hot reload support (proxy list and settings)
    """

    def __init__(self):
        self._proxies: Dict[str, ProxyInfo] = {}  # proxy_id -> ProxyInfo
        self._queue: deque = deque()  # Rotation queue for sequential
        self._shuffle_queue: deque = deque()  # Shuffle queue for random_no_repeat
        self._current: Optional[ProxyInfo] = None  # Current global proxy
        self._lock: asyncio.Lock = asyncio.Lock()
        self._rotation_task: Optional[asyncio.Task] = None
        self._settings: Optional[ProxySettings] = None
        self._mode: ProxyMode = ProxyMode.DISABLED  # Current effective mode
        self._last_probe_result: Dict[str, str] = {}  # account_id -> proxy_id for log deduplication

    @property
    def proxy_count(self) -> int:
        """Get total number of proxies."""
        return len(self._proxies)

    @property
    def available_count(self) -> int:
        """Get number of available (healthy) proxies."""
        return sum(1 for p in self._proxies.values() if p.is_available)

    async def initialize(self) -> None:
        """
        初始化代理池服务
        Initialize proxy pool from configuration and file.
        """
        # Get settings
        self._settings = settings.proxy
        self._mode = settings.effective_proxy_mode

        # Log initialization based on mode
        if self._mode == ProxyMode.DISABLED:
            logger.info("Proxy service initialized: mode=disabled")
            return

        if self._mode == ProxyMode.FIXED:
            fixed_url = settings.effective_fixed_url
            if fixed_url:
                # Mask credentials in URL for logging
                try:
                    parsed = urlparse(fixed_url)
                    safe_url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
                except Exception:
                    safe_url = "[invalid URL]"
                logger.info(f"Proxy service initialized: mode=fixed, proxy={safe_url}")
            else:
                logger.info("Proxy service initialized: mode=fixed, proxy=None (no proxy will be used)")
            return

        # Dynamic mode
        if self._settings is None:
            logger.warning("Dynamic mode enabled but no proxy settings configured")
            return

        # Load proxies from file
        count = await self.load_from_file()
        strategy = self._settings.rotation_strategy.value
        logger.info(
            f"Proxy service initialized: mode=dynamic, "
            f"proxies={count}, strategy={strategy}"
        )

        # Start rotation task if needed
        if self._settings.rotation_strategy != RotationStrategy.PER_ACCOUNT:
            await self.start_rotation_task()

    async def shutdown(self) -> None:
        """
        关闭代理池服务
        Shutdown proxy pool service.
        """
        await self.stop_rotation_task()
        logger.info("Proxy pool service shutdown")

    async def load_from_file(self) -> int:
        """
        从文件加载代理列表
        Load proxy list from file.

        Returns:
            Number of proxies loaded.
        """
        proxies_file = settings.data_folder / "proxies.txt"

        if not proxies_file.exists():
            logger.warning(f"Proxies file not found: {proxies_file}")
            return 0

        try:
            content = proxies_file.read_text(encoding="utf-8")
            return await self.load_from_content(content)
        except Exception as e:
            logger.error(f"Failed to load proxies file: {e}")
            return 0

    async def load_from_content(self, content: str) -> int:
        """
        从内容加载代理列表（支持热更新）
        Load proxy list from content string (supports hot reload).

        Returns:
            Number of proxies loaded.
        """
        proxies = ProxyParser.parse_content(content)

        async with self._lock:
            # Build new proxy dict
            new_proxies: Dict[str, ProxyInfo] = {}
            for proxy in proxies:
                proxy_id = proxy.proxy_id
                # Preserve health state for existing proxies
                if proxy_id in self._proxies:
                    proxy.cooldown_until = self._proxies[proxy_id].cooldown_until
                new_proxies[proxy_id] = proxy

            # Update proxies
            self._proxies = new_proxies

            # Rebuild rotation queue
            self._rebuild_queue()

            # 选择第一个健康代理作为初始代理（不是轮换）
            self._select_initial_proxy()

        count = len(self._proxies)

        if count == 0:
            logger.warning(
                "Dynamic proxy mode enabled but proxy list is empty, "
                "requests will use direct connection without proxy"
            )
        else:
            logger.info(f"Loaded {count} proxies")

        return count

    def _rebuild_queue(self) -> None:
        """
        重建轮换队列
        Rebuild rotation queue based on strategy.
        """
        if not self._settings:
            return

        proxy_ids = list(self._proxies.keys())
        self._queue = deque(proxy_ids)

        # 重置 RANDOM_NO_REPEAT 专用队列
        self._shuffle_queue.clear()

    async def get_proxy(
        self,
        account_id: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> Optional[str]:
        """
        获取代理 URL
        Get proxy URL based on current mode and strategy.

        Args:
            account_id: Account identifier (organization_uuid) for per_account strategy.
            cookie: Cookie string for generating temporary account_id.

        Returns:
            Proxy URL string or None if no proxy available.
        """
        mode = settings.effective_proxy_mode

        # Disabled mode: no proxy
        if mode == ProxyMode.DISABLED:
            return None

        # Fixed mode: return fixed URL
        if mode == ProxyMode.FIXED:
            return settings.effective_fixed_url

        # Dynamic mode: use proxy pool
        return await self._get_dynamic_proxy(account_id, cookie)

    async def _get_dynamic_proxy(
        self,
        account_id: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> Optional[str]:
        """Get proxy from dynamic pool."""
        if not self._settings or not self._proxies:
            return None

        strategy = self._settings.rotation_strategy

        # per_account strategy
        if strategy == RotationStrategy.PER_ACCOUNT:
            return await self._get_per_account_proxy(account_id, cookie)

        # Other strategies: return current global proxy
        async with self._lock:
            if self._current and self._current.is_available:
                return self._current.url

            # Current proxy unavailable, try to rotate to next available
            if await self._do_rotate():
                return self._current.url if self._current else None

            # All proxies unavailable - raise exception instead of returning None
            raise AllProxiesUnavailableError(context={
                "total": len(self._proxies),
                "strategy": self._settings.rotation_strategy.value,
            })

    async def _get_per_account_proxy(
        self,
        account_id: Optional[str] = None,
        cookie: Optional[str] = None,
    ) -> Optional[str]:
        """
        获取 per_account 策略的代理（哈希映射 + 线性探测）
        Get proxy using hash-based mapping with linear probing.

        映射规则：
        - 使用 hash(account_id) % len(proxies) 计算基础索引
        - 如果基础索引的代理不健康，线性探测找下一个健康代理
        - 代理恢复健康后，下次请求自动回到原始哈希位置
        - 映射只在代理列表变化时改变
        """
        if not self._settings:
            return None

        # 确定有效的账户标识
        effective_account_id = account_id
        if not effective_account_id and cookie:
            # Use cookie hash as temporary account_id
            effective_account_id = f"cookie_{hashlib.md5(cookie.encode()).hexdigest()[:16]}"

        if not effective_account_id:
            # No account_id available, fallback to fallback_strategy
            return await self._get_fallback_proxy()

        # 哈希映射 + 线性探测
        async with self._lock:
            proxies_list = list(self._proxies.values())
            if not proxies_list:
                return None

            base_index = hash(effective_account_id) % len(proxies_list)

            # 线性探测找第一个健康代理
            for offset in range(len(proxies_list)):
                index = (base_index + offset) % len(proxies_list)
                proxy = proxies_list[index]
                if proxy.is_available:
                    # Only log when probing (offset > 0) and result changed
                    if offset > 0:
                        last_proxy_id = self._last_probe_result.get(effective_account_id)
                        if last_proxy_id != proxy.proxy_id:
                            logger.info(
                                f"Account {effective_account_id[:16]}... -> {proxy.url_safe} "
                                f"(hash={base_index}, probed +{offset})"
                            )
                    # Track the probe result for log deduplication
                    self._last_probe_result[effective_account_id] = proxy.proxy_id
                    return proxy.url

            # 所有代理不可用 - 抛出异常（区别于空列表直连）
            logger.error(
                f"All proxies unavailable for account {effective_account_id[:16]}..., "
                f"total={len(proxies_list)}"
            )
            raise AllProxiesUnavailableError(context={
                "total": len(proxies_list),
                "account_id": effective_account_id[:16],
            })

    # ==================== 统一轮换逻辑 ====================

    async def _do_rotate(self, strategy: Optional[RotationStrategy] = None) -> bool:
        """
        统一轮换动作（主动和被动共用）
        Unified rotation action for both active and passive rotation.

        Args:
            strategy: 指定使用的策略，None 则使用当前配置的策略

        Returns:
            True if rotation succeeded, False if all proxies unavailable
        """
        if not self._settings or not self._proxies:
            return False

        # 支持指定策略（用于 fallback）
        use_strategy = strategy or self._settings.rotation_strategy

        if use_strategy == RotationStrategy.SEQUENTIAL:
            success = self._rotate_sequential()
        elif use_strategy == RotationStrategy.RANDOM:
            success = self._rotate_random()
        elif use_strategy == RotationStrategy.RANDOM_NO_REPEAT:
            success = self._rotate_random_no_repeat()
        else:
            return False

        return success

    def _rotate_sequential(self) -> bool:
        """
        顺序轮换：队列移动一位，找下一个健康的
        Sequential rotation: move queue position and find next healthy proxy.
        """
        if not self._queue:
            return False

        self._queue.rotate(-1)
        return self._select_next_healthy_from_queue()

    def _rotate_random(self) -> bool:
        """
        随机轮换：从所有健康代理中随机选
        Random rotation: randomly select from all healthy proxies.
        """
        available = [p for p in self._proxies.values() if p.is_available]
        if not available:
            return False
        self._current = random.choice(available)
        return True

    def _rotate_random_no_repeat(self) -> bool:
        """
        随机不重复：从洗牌队列依次取，用完重新洗
        Random no repeat: take from shuffled queue, reshuffle when empty.
        """
        while True:
            # 队列空了就重新洗牌（只洗健康的）
            if not self._shuffle_queue:
                available_ids = [pid for pid, p in self._proxies.items() if p.is_available]
                if not available_ids:
                    return False
                random.shuffle(available_ids)
                self._shuffle_queue = deque(available_ids)
                logger.debug(f"RANDOM_NO_REPEAT: reshuffled {len(available_ids)} healthy proxies")

            # 取一个
            proxy_id = self._shuffle_queue.popleft()
            proxy = self._proxies.get(proxy_id)

            # 健康就用，不健康就丢弃继续
            if proxy and proxy.is_available:
                self._current = proxy
                return True
            # 不健康的被丢弃，继续取下一个（会触发重新洗牌如果队列空了）

    def _select_next_healthy_from_queue(self) -> bool:
        """
        从 _queue 当前位置开始找健康代理（SEQUENTIAL 专用）
        Find next healthy proxy from current queue position (for SEQUENTIAL).
        """
        if not self._queue:
            return False

        for _ in range(len(self._queue)):
            proxy_id = self._queue[0]
            proxy = self._proxies.get(proxy_id)
            if proxy and proxy.is_available:
                self._current = proxy
                return True
            self._queue.rotate(-1)
        return False

    def _select_initial_proxy(self) -> bool:
        """
        选择队列第一个健康代理作为初始代理（不移动队列位置）
        Select first healthy proxy from queue as initial proxy (without moving queue position).
        """
        if not self._queue:
            self._current = None
            return False

        for proxy_id in self._queue:
            proxy = self._proxies.get(proxy_id)
            if proxy and proxy.is_available:
                self._current = proxy
                return True

        self._current = None
        return False

    async def _get_fallback_proxy(self) -> Optional[str]:
        """
        获取回退策略的代理
        Get proxy using fallback strategy (when no account_id in PER_ACCOUNT mode).
        """
        if not self._settings:
            return None

        # 使用配置的 fallback_strategy 进行轮换
        async with self._lock:
            # 如果当前代理健康，直接返回
            if self._current and self._current.is_available:
                return self._current.url
            # 否则使用回退策略轮换
            fallback = self._settings.fallback_strategy
            if await self._do_rotate(strategy=fallback):
                return self._current.url if self._current else None

            # All proxies unavailable - raise exception instead of returning None
            raise AllProxiesUnavailableError(context={
                "total": len(self._proxies),
                "strategy": f"fallback:{fallback.value}",
            })

    async def mark_unhealthy(self, proxy_url: str, reason: str = "unknown") -> None:
        """
        标记代理为不健康状态
        Mark proxy as unhealthy and enter cooldown period.

        Args:
            proxy_url: The proxy URL to mark as unhealthy.
            reason: The reason for marking unhealthy (e.g., "HTTP 403", "connection timeout").
        """
        if not self._settings:
            return

        # Extract proxy_id from URL
        try:
            parsed = urlparse(proxy_url)
            proxy_id = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        except Exception:
            logger.warning(f"Invalid proxy URL for marking unhealthy: {proxy_url[:50]}...")
            return

        async with self._lock:
            proxy = self._proxies.get(proxy_id)
            if not proxy:
                logger.warning(f"Proxy not found in pool: {proxy_id}")
                return

            # Mark unhealthy
            proxy.mark_unhealthy(self._settings.cooldown_duration)
            logger.warning(
                f"Proxy {proxy.url_safe} marked unhealthy (reason: {reason}), "
                f"cooldown until {proxy.cooldown_until}"
            )

            # If current proxy became unhealthy, trigger passive rotation
            # 注意：PER_ACCOUNT 策略不需要更新 _current，下次请求会重新计算
            if self._current and self._current.proxy_id == proxy_id:
                strategy = self._settings.rotation_strategy
                if strategy != RotationStrategy.PER_ACCOUNT:
                    old_proxy = proxy.url_safe
                    if await self._do_rotate():
                        logger.warning(
                            f"Passive rotation: {old_proxy} -> {self._current.url_safe} "
                            f"(reason: {reason})"
                        )
                    else:
                        logger.error(
                            f"Passive rotation failed: no available proxy after {old_proxy} failure "
                            f"(reason: {reason})"
                        )

    async def start_rotation_task(self) -> None:
        """
        启动定时轮换任务
        Start the periodic rotation task (for non-per_account strategies).
        """
        if self._rotation_task and not self._rotation_task.done():
            return

        if not self._settings:
            return

        if self._settings.rotation_strategy == RotationStrategy.PER_ACCOUNT:
            return

        self._rotation_task = asyncio.create_task(self._rotation_loop())
        logger.info(
            f"Started rotation task with interval {self._settings.rotation_interval}s, "
            f"strategy: {self._settings.rotation_strategy.value}"
        )

    async def stop_rotation_task(self) -> None:
        """
        停止定时轮换任务
        Stop the periodic rotation task.
        """
        if self._rotation_task and not self._rotation_task.done():
            self._rotation_task.cancel()
            try:
                await self._rotation_task
            except asyncio.CancelledError:
                pass
            self._rotation_task = None
            logger.info("Rotation task stopped")

    async def _rotation_loop(self) -> None:
        """
        定时轮换循环
        Rotation loop for periodic proxy switching.

        每次循环都读取最新的 rotation_interval，使间隔更新能立即生效。
        """
        while True:
            try:
                # 每次都读取最新配置的轮换间隔（支持热更新）
                interval = self._settings.rotation_interval if self._settings else 300
                await asyncio.sleep(interval)
                await self._rotate_proxy()
            except asyncio.CancelledError:
                logger.debug("Rotation loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in rotation loop: {e}")
                await asyncio.sleep(60)  # 出错时等待 60 秒再重试

    async def _rotate_proxy(self) -> None:
        """
        执行一次轮换（主动轮换，由定时器触发）
        Perform one rotation step (active rotation by timer).
        """
        if not self._settings:
            return

        async with self._lock:
            if not self._proxies:
                return

            strategy = self._settings.rotation_strategy
            old_proxy = self._current.url_safe if self._current else None

            if await self._do_rotate():
                if self._current and old_proxy != self._current.url_safe:
                    logger.info(
                        f"Proxy rotated: {old_proxy} -> {self._current.url_safe} "
                        f"(strategy: {strategy.value})"
                    )
            else:
                logger.error(f"Rotation failed: all proxies unavailable (strategy: {strategy.value})")

    def get_status(self) -> Dict:
        """
        获取代理池状态
        Get proxy pool status for monitoring/debugging.
        """
        return {
            "mode": settings.effective_proxy_mode.value,
            "total": self.proxy_count,
            "available": self.available_count,
            "current": self._current.url_safe if self._current else None,
            "strategy": self._settings.rotation_strategy.value if self._settings else None,
        }

    # ==================== 配置热更新 ====================

    async def _start_rotation_task_internal(self) -> None:
        """
        启动轮换任务（内部方法，调用者需持有锁）
        Start rotation task (internal, caller must hold lock).
        """
        if self._rotation_task and not self._rotation_task.done():
            return

        if not self._settings:
            return

        if self._settings.rotation_strategy == RotationStrategy.PER_ACCOUNT:
            return

        self._rotation_task = asyncio.create_task(self._rotation_loop())
        logger.debug("Rotation task started (internal)")

    async def _stop_rotation_task_internal(self) -> None:
        """
        停止轮换任务（内部方法，调用者需持有锁）
        Stop rotation task (internal, caller must hold lock).
        """
        if self._rotation_task and not self._rotation_task.done():
            self._rotation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rotation_task
            self._rotation_task = None
            logger.debug("Rotation task stopped (internal)")

    async def reload_settings(self) -> None:
        """
        重新加载代理服务配置（配置更新后调用）
        Reload proxy settings after configuration update.

        处理以下配置变更：
        - proxy.mode: 模式切换（disabled/fixed/dynamic）
        - proxy.rotation_strategy: 轮换策略
        - proxy.rotation_interval: 轮换间隔
        - proxy.cooldown_duration: 冷却时间
        - proxy.fallback_strategy: 回退策略
        """
        async with self._lock:
            old_mode = self._mode
            old_strategy = self._settings.rotation_strategy if self._settings else None

            # 重新读取配置
            self._settings = settings.proxy
            self._mode = settings.effective_proxy_mode

            logger.info(
                f"Reloading proxy settings: mode={self._mode.value}, "
                f"strategy={self._settings.rotation_strategy.value if self._settings else 'N/A'}, "
                f"interval={self._settings.rotation_interval if self._settings else 'N/A'}s"
            )

            # 根据模式决定是否需要轮换任务
            if self._mode != ProxyMode.DYNAMIC:
                # 非动态模式：停止轮换任务
                await self._stop_rotation_task_internal()
                if old_mode == ProxyMode.DYNAMIC:
                    logger.info(f"Rotation task stopped: mode changed to {self._mode.value}")
                return

            # 动态模式：检查是否需要重建队列（策略变更可能需要）
            new_strategy = self._settings.rotation_strategy

            if old_strategy != new_strategy:
                # 策略变更，重建队列并选择初始代理
                self._rebuild_queue()
                if not self._select_initial_proxy():
                    logger.warning("Strategy changed but all proxies unavailable")

            if new_strategy == RotationStrategy.PER_ACCOUNT:
                # PER_ACCOUNT 不需要轮换任务
                await self._stop_rotation_task_internal()
                if old_strategy != RotationStrategy.PER_ACCOUNT:
                    logger.info("Rotation task stopped: strategy changed to PER_ACCOUNT")
            else:
                # 需要轮换任务：重启以应用新配置
                await self._stop_rotation_task_internal()
                await self._start_rotation_task_internal()
                logger.info(
                    f"Rotation task restarted: strategy={new_strategy.value}, "
                    f"interval={self._settings.rotation_interval}s"
                )


# 全局代理池实例
# Global proxy pool instance
proxy_service = ProxyPool()
