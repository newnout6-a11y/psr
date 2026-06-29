from .tls_client import TLSClient

__all__ = ["TLSClient", "OriginFinder", "APIReverser"]


def __getattr__(name: str):
    if name == "OriginFinder":
        from .origin_finder import OriginFinder

        return OriginFinder
    if name == "APIReverser":
        from .api_reverser import APIReverser

        return APIReverser
    raise AttributeError(name)
