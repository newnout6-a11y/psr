# Уровень 3: Взаимодействие
from .proposal_templates import generate_proposal
from .proposal_db import ProposalDB

__all__ = ["ProposalSender", "generate_proposal", "ProposalDB"]


def __getattr__(name: str):
    if name == "ProposalSender":
        from .proposal_sender import ProposalSender

        return ProposalSender
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
