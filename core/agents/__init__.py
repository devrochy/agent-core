"""Core playbooks: no todo puede hacerse con una variable en un `__init__.py`."""

from core.agents.playbooks import load_playbook

__all__ = ["load_playbook"]
