from django import forms
from django.contrib import admin

from .models import Account, ModelPrice, RequestLog, UsageWindow


class AccountAdminForm(forms.ModelForm):
    credential_plain = forms.CharField(
        label="上游凭据",
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="OAuth 填 access token；API Key 填 key。留空表示保持原值。",
    )

    class Meta:
        model = Account
        exclude = ("credential", "inflight")

    def save(self, commit: bool = True):
        account = super().save(commit=False)
        raw = self.cleaned_data.get("credential_plain")
        if raw:
            account.set_credential(raw)
        if commit:
            account.save()
            self.save_m2m()
        return account


class UsageWindowInline(admin.TabularInline):
    model = UsageWindow
    extra = 0
    fields = ("key", "utilization", "status", "resets_at", "cost_usd", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    form = AccountAdminForm
    list_display = (
        "name",
        "channel",
        "tier",
        "auth_type",
        "quota_mode",
        "utilization_display",
        "available",
        "enabled",
        "priority",
        "inflight",
    )
    list_filter = ("tier", "quota_mode", "auth_type", "enabled")
    search_fields = ("name", "base_url")
    inlines = [UsageWindowInline]

    @admin.display(description="已用比例")
    def utilization_display(self, obj: Account) -> str:
        return f"{obj.utilization():.2%}"

    @admin.display(description="可用", boolean=True)
    def available(self, obj: Account) -> bool:
        return obj.is_available()


@admin.register(ModelPrice)
class ModelPriceAdmin(admin.ModelAdmin):
    list_display = (
        "model_prefix",
        "input_per_mtok",
        "output_per_mtok",
        "cache_read_per_mtok",
        "cache_write_per_mtok",
    )


@admin.register(RequestLog)
class RequestLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "channel",
        "account_name",
        "model",
        "status_code",
        "blocked_reason",
        "input_tokens",
        "output_tokens",
        "cost_usd",
    )
    list_filter = ("channel", "tier", "status_code", "model")
    search_fields = ("request_id", "model", "blocked_reason")
    readonly_fields = [field.name for field in RequestLog._meta.fields]

    def has_add_permission(self, request):
        return False
