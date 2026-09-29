"""ToolGate — a tool-call authorization guardrail (MCP gateway, v1 MVP).

Layers: ``domain`` (pure model + rules) → ``application`` (use cases) →
``infrastructure`` (adapters to Cedar / Jev / disk) → ``interfaces`` (entry points).
"""

from .application.engine import Engine
from .domain.model import AuthzResult, Budget, Principal
from .infrastructure.authorizer import CedarEmbeddedAuthorizer

__all__ = ["Engine", "Principal", "Budget", "AuthzResult", "CedarEmbeddedAuthorizer"]
