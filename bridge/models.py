"""Data model for the CC Switch bridge.

An *account* is one upstream credential.  Official subscription accounts use
the ``headers`` quota mode (Anthropic's ``anthropic-ratelimit-unified-*``
response headers); API-key accounts use ``budget``; third-party providers use
``probe`` (an HTTP balance/usage endpoint) or ``none``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from .crypto import decrypt, encrypt


def default_probe() -> dict:
    """Sensible default for DeepSeek-style ``/user/balance`` relays."""
    return {
        "url": "{base_url}/user/balance",
        "method": "GET",
        "headers": {"Authorization": "Bearer {api_key}"},
        "remaining_paths": ["balance", "balance_infos[*].total_balance"],
        "total_path": None,
        "active_path": None,
    }


class Account(models.Model):
    TIER_OFFICIAL = "official"
    TIER_THIRD_PARTY = "third_party"
    TIER_CHOICES = [
        (TIER_OFFICIAL, "官方账号"),
        (TIER_THIRD_PARTY, "第三方供应商"),
    ]

    PROTOCOL_ANTHROPIC = "anthropic"
    PROTOCOL_OPENAI = "openai"
    PROTOCOL_CODEX = "codex"
    PROTOCOL_CHOICES = [
        (PROTOCOL_ANTHROPIC, "Anthropic /v1/messages"),
        (PROTOCOL_OPENAI, "OpenAI /v1/responses"),
        (PROTOCOL_CODEX, "ChatGPT Codex 官方"),
    ]

    AUTH_OAUTH = "oauth"
    AUTH_API_KEY = "api_key"
    AUTH_CHOICES = [
        (AUTH_OAUTH, "OAuth (Claude Code 订阅)"),
        (AUTH_API_KEY, "API Key"),
    ]

    QUOTA_HEADERS = "headers"
    QUOTA_BUDGET = "budget"
    QUOTA_PROBE = "probe"
    QUOTA_OFFICIAL = "official"
    QUOTA_NONE = "none"
    QUOTA_CHOICES = [
        (QUOTA_HEADERS, "上游响应头 (订阅额度)"),
        (QUOTA_BUDGET, "本地预算 (按量计费)"),
        (QUOTA_PROBE, "HTTP 查询 (第三方余额)"),
        (QUOTA_OFFICIAL, "官方 ChatGPT 用量 (wham/usage)"),
        (QUOTA_NONE, "不限制"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField("名称", max_length=100, unique=True)
    channel = models.CharField(
        "渠道标签", max_length=100, blank=True, default="",
        help_text="用于日志区分，例如「官方」「pp 主渠道」。",
    )
    cc_provider_id = models.CharField(
        "CC Switch provider id", max_length=64, blank=True, default="",
        help_text="镜像日志到 CC Switch 时使用的供应商 id。",
    )
    tier = models.CharField(
        "层级", max_length=20, choices=TIER_CHOICES, default=TIER_OFFICIAL
    )
    protocol = models.CharField(
        "上游协议", max_length=20, choices=PROTOCOL_CHOICES, default=PROTOCOL_ANTHROPIC
    )
    auth_type = models.CharField(
        "认证方式", max_length=20, choices=AUTH_CHOICES, default=AUTH_OAUTH
    )
    base_url = models.URLField("上游地址", default="https://api.anthropic.com")
    credential = models.TextField("凭据 (加密)", blank=True, default="")

    quota_mode = models.CharField(
        "额度模式", max_length=20, choices=QUOTA_CHOICES, default=QUOTA_HEADERS
    )
    budget_usd = models.DecimalField(
        "预算/额度参考值 (USD)",
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="budget 模式表示预算上限；probe 模式可作为剩余额度换算的分母。",
    )
    quota_probe = models.JSONField(
        "第三方额度查询配置",
        default=default_probe,
        blank=True,
        help_text="支持 {base_url} / {api_key} 占位符，remaining_paths 支持 a.b 和 a[*].b。",
    )
    reserve_ratio = models.DecimalField(
        "保留比例", max_digits=5, decimal_places=4, default=Decimal("0.05")
    )
    safety_margin = models.DecimalField(
        "安全余量", max_digits=5, decimal_places=4, default=Decimal("0.005")
    )

    enabled = models.BooleanField("启用", default=True)
    priority = models.IntegerField("优先级 (越小越先用)", default=100)
    price_multiplier = models.DecimalField(
        "价格系数", max_digits=6, decimal_places=4, default=Decimal("1.0"),
        help_text="按量计费的实际单价系数，用于动态路由比价。",
    )
    max_inflight = models.PositiveIntegerField("最大并发", default=4)
    inflight = models.PositiveIntegerField("当前并发", default=0)
    cooldown_until = models.DateTimeField("冷却截止", null=True, blank=True)
    last_error = models.TextField("最近错误", blank=True, default="")

    # Rolling metrics used by dynamic routing.
    ewma_ttft_ms = models.FloatField("首字延迟 EWMA (ms)", null=True, blank=True)
    ewma_tps = models.FloatField("生成速度 EWMA (tok/s)", null=True, blank=True)
    ewma_cost_per_1k = models.DecimalField(
        "每千 token 成本 EWMA (USD)", max_digits=12, decimal_places=6,
        null=True, blank=True,
    )
    ewma_success = models.FloatField("成功率 EWMA", null=True, blank=True)
    stats_samples = models.PositiveIntegerField("样本数", default=0)
    stats_updated_at = models.DateTimeField("指标更新时间", null=True, blank=True)
    observed_cost_per_1k = models.DecimalField(
        "供应商实际每千 token 成本", max_digits=12, decimal_places=6,
        null=True, blank=True,
    )
    last_slow_at = models.DateTimeField("最近慢请求", null=True, blank=True)
    last_used_at = models.DateTimeField("最近使用", null=True, blank=True)

    blocked_models = models.JSONField("不支持的模型", default=list, blank=True)
    extra_headers = models.JSONField("附加请求头", default=dict, blank=True)
    last_probe_at = models.DateTimeField("最近查询时间", null=True, blank=True)
    last_probe_error = models.TextField("最近查询错误", blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tier", "priority", "name"]
        verbose_name = "账号"
        verbose_name_plural = "账号"

    def __str__(self) -> str:
        return f"{self.name} ({self.get_tier_display()})"

    # -- credentials -----------------------------------------------------
    def set_credential(self, raw: str) -> None:
        self.credential = encrypt(raw) if raw else ""

    @property
    def credential_plain(self) -> str:
        return decrypt(self.credential) if self.credential else ""

    # -- quota -----------------------------------------------------------
    def window_map(self) -> dict[str, "UsageWindow"]:
        return {w.key: w for w in self.windows.all()}

    def quota_limit(self) -> Decimal:
        """Utilisation at which the account must stop being used."""
        limit = Decimal("1") - self.reserve_ratio - self.safety_margin
        return max(Decimal("0"), limit)

    def relevant_windows(self) -> list["UsageWindow"]:
        windows = self.windows.all()
        if self.quota_mode == self.QUOTA_HEADERS:
            return [w for w in windows if w.key in (UsageWindow.FIVE_HOURS, UsageWindow.SEVEN_DAYS)]
        if self.quota_mode == self.QUOTA_BUDGET:
            return [w for w in windows if w.key == UsageWindow.BUDGET]
        if self.quota_mode == self.QUOTA_PROBE:
            return [w for w in windows if w.key == UsageWindow.PROBE]
        if self.quota_mode == self.QUOTA_OFFICIAL:
            return [
                w
                for w in windows
                if w.key in (UsageWindow.OFFICIAL_5H, UsageWindow.OFFICIAL_7D)
            ]
        return []

    def utilization(self) -> Decimal:
        values = [w.utilization for w in self.relevant_windows()]
        return max(values) if values else Decimal("0")

    def at_reserve(self) -> bool:
        if self.quota_mode == self.QUOTA_NONE:
            return False
        return self.utilization() >= self.quota_limit()

    def remaining_ratio(self) -> Decimal:
        return max(Decimal("0"), Decimal("1") - self.utilization())

    def is_available(self, now=None) -> bool:
        now = now or timezone.now()
        if not self.enabled:
            return False
        if self.inflight >= self.max_inflight:
            return False
        if self.cooldown_until and self.cooldown_until > now:
            return False
        if self.quota_mode == self.QUOTA_BUDGET and not self.budget_usd:
            return False
        if self.quota_mode == self.QUOTA_PROBE:
            has_probe = any(w.key == UsageWindow.PROBE for w in self.windows.all())
            if settings.BRIDGE.get("PROBE_REQUIRED", True) and not has_probe:
                return False
        if self.quota_mode == self.QUOTA_OFFICIAL:
            # Fail closed: never spend official quota before we know its usage.
            has_official = any(
                w.key in (UsageWindow.OFFICIAL_5H, UsageWindow.OFFICIAL_7D)
                for w in self.windows.all()
            )
            if not has_official:
                return False
        return not self.at_reserve()


class UsageWindow(models.Model):
    FIVE_HOURS = "5h"
    SEVEN_DAYS = "7d"
    BUDGET = "budget"
    PROBE = "probe"
    OFFICIAL_5H = "official_5h"
    OFFICIAL_7D = "official_7d"
    KEY_CHOICES = [
        (FIVE_HOURS, "5 小时窗口"),
        (SEVEN_DAYS, "每周窗口"),
        (BUDGET, "预算周期"),
        (PROBE, "第三方余额"),
        (OFFICIAL_5H, "官方 5 小时窗口"),
        (OFFICIAL_7D, "官方每周窗口"),
    ]

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="windows")
    key = models.CharField(max_length=20, choices=KEY_CHOICES)
    utilization = models.DecimalField(
        "已用比例", max_digits=7, decimal_places=6, default=Decimal("0")
    )
    status = models.CharField("上游状态", max_length=40, blank=True, default="")
    resets_at = models.DateTimeField("重置时间", null=True, blank=True)
    cost_usd = models.DecimalField(
        "周期内花费 (USD)", max_digits=12, decimal_places=6, default=Decimal("0")
    )
    raw = models.JSONField("原始数据", default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("account", "key")]
        verbose_name = "额度窗口"
        verbose_name_plural = "额度窗口"

    def __str__(self) -> str:
        return f"{self.account.name}:{self.key}={self.utilization}"


class RequestLog(models.Model):
    account = models.ForeignKey(
        Account, on_delete=models.SET_NULL, null=True, blank=True, related_name="requests"
    )
    account_name = models.CharField(max_length=100, blank=True, default="")
    channel = models.CharField(max_length=100, blank=True, default="")
    tier = models.CharField(max_length=20, blank=True, default="")
    base_url = models.CharField(max_length=255, blank=True, default="")
    request_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    path = models.CharField(max_length=255, blank=True, default="")
    model = models.CharField(max_length=120, blank=True, default="")
    status_code = models.IntegerField(null=True, blank=True)
    blocked_reason = models.CharField(max_length=120, blank=True, default="")
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cache_read_tokens = models.IntegerField(default=0)
    cache_write_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )
    duration_ms = models.IntegerField(null=True, blank=True)
    ttft_ms = models.IntegerField(null=True, blank=True)
    tps = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "请求日志"
        verbose_name_plural = "请求日志"

    def __str__(self) -> str:
        return f"{self.request_id} {self.status_code} {self.model}"


class ModelPrice(models.Model):
    """USD per million tokens, used by the local ``budget`` quota mode."""

    model_prefix = models.CharField("模型前缀", max_length=120, unique=True)
    input_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    output_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    cache_read_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    cache_write_per_mtok = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-model_prefix"]
        verbose_name = "模型价格"
        verbose_name_plural = "模型价格"

    def __str__(self) -> str:
        return self.model_prefix
