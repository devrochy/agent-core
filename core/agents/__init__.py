"""Core playbooks: no todo puede hacerse con una variable en un `__init__.py`."""

from core.agents.playbooks import load_playbook
from core.agents.spec import ensure_default_spec, load_active_spec

__all__ = ["ensure_default_spec", "load_active_spec", "load_playbook"]