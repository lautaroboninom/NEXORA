from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings


@dataclass(frozen=True)
class BejermanIngressCompany:
    key: str
    label: str
    branding_key: str
    bejerman_company: str
    is_test: bool = False

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "brandingKey": self.branding_key,
            "isTest": self.is_test,
        }


DEFAULT_INGRESS_COMPANY_KEY = "SEPID"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalize_key(value: Any) -> str:
    return _clean(value).replace("-", "").replace("_", "").replace(" ", "").upper()


def _configured_companies() -> tuple[BejermanIngressCompany, ...]:
    return (
        BejermanIngressCompany(
            key="SEPID",
            label="SEPID SA",
            branding_key="SEPID",
            bejerman_company=_clean(getattr(settings, "BEJERMAN_COMPANY_SEPID", "SEP")) or "SEP",
        ),
        BejermanIngressCompany(
            key="MGBIO",
            label="MG BIO",
            branding_key="MGBIO",
            bejerman_company=_clean(getattr(settings, "BEJERMAN_COMPANY_MGBIO", "MGBI")) or "MGBI",
        ),
        BejermanIngressCompany(
            key="TEST",
            label="Empresa de prueba",
            branding_key="TEST",
            bejerman_company=_clean(getattr(settings, "BEJERMAN_COMPANY_TEST", "MODE")) or "MODE",
            is_test=True,
        ),
    )


def list_ingress_companies() -> list[BejermanIngressCompany]:
    return list(_configured_companies())


def company_for_key(value: Any, *, default: str | None = DEFAULT_INGRESS_COMPANY_KEY) -> BejermanIngressCompany | None:
    normalized = _normalize_key(value)
    if not normalized and default:
        normalized = _normalize_key(default)

    aliases = {
        "SEP": "SEPID",
        "SEPIDSA": "SEPID",
        "CEPIL": "SEPID",
        "MGBI": "MGBIO",
        "MGBIO": "MGBIO",
        "MGBIOSA": "MGBIO",
        "MODE": "TEST",
        "PRUEBA": "TEST",
        "TEST": "TEST",
    }
    normalized = aliases.get(normalized, normalized)
    for company in _configured_companies():
        if company.key == normalized:
            return company
    return None


def require_company(value: Any, *, default: str | None = DEFAULT_INGRESS_COMPANY_KEY) -> BejermanIngressCompany:
    company = company_for_key(value, default=default)
    if not company:
        raise ValueError("Empresa Bejerman no válida")
    return company
