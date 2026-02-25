from __future__ import annotations

# Backward-compatible alias after migrating from Dome to PMXT.
from agenttrader.data.pmxt_client import PmxtClient


DomeClient = PmxtClient

__all__ = ["PmxtClient", "DomeClient"]
