"""Source-specific pure parsing and validation adapters."""

from .web_catalog import WEB_CATALOG_CAPABILITIES, KworkWebCatalogAdapter, WebCatalogSource
from .mobile_kworks import KworkMobileKworksAdapter

__all__ = [
    "WEB_CATALOG_CAPABILITIES",
    "KworkMobileKworksAdapter",
    "KworkWebCatalogAdapter",
    "WebCatalogSource",
]
