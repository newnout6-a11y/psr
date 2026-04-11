# Уровень 3: Взаимодействие
from .proposal_templates import generate_proposal
from .proposal_db import ProposalDB
from .proposal_sender import ProposalSender

__all__ = ["ProposalSender", "generate_proposal", "ProposalDB"]
