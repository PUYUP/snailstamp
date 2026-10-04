"""`manage.py check --deploy`: peringatan konfigurasi integritas ledger untuk produksi."""
from django.conf import settings
from django.core import checks


@checks.register(checks.Tags.security, deploy=True)
def ledger_deploy_checks(app_configs, **kwargs):
    issues = []
    if not getattr(settings, "LEDGER_REQUIRE_BLOCK_SIGNATURES", True):
        issues.append(checks.Error("LEDGER_REQUIRE_BLOCK_SIGNATURES=False: blok tanpa signature diterima",
                                   id="ledger.E001"))
    threshold = getattr(settings, "BLOCK_SIGNATURE_THRESHOLD", 1) or 1
    regions = getattr(settings, "LEDGER_SEALER_MIN_REGIONS", 1) or 1
    if threshold < 2 or regions < 2:
        issues.append(checks.Warning(
            f"Threshold signature {threshold} dari {regions} region: satu key bocor cukup untuk memalsukan blok",
            hint="Set BLOCK_SIGNATURE_THRESHOLD=2 dan LEDGER_SEALER_MIN_REGIONS=2 (co-sign lewat cosign_ledger)",
            id="ledger.W001"))
    if not getattr(settings, "LEDGER_ANCHOR_BACKENDS", None):
        issues.append(checks.Warning("LEDGER_ANCHOR_BACKENDS kosong: checkpoint tidak di-anchor ke luar DB",
                                     hint="Set LEDGER_ANCHOR_FILE / LEDGER_ANCHOR_URL", id="ledger.W002"))
    return issues
