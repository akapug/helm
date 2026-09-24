"""Identity-estate path overrides shared by runtime and test isolation.

This is deliberately a six-name set, not a registry of Helm's environment.
Only the three paths that can split one identity estate belong here: the Helm
home, the chat root, and the Actor store.  The module stays dependency-free so
``gateshard.py`` and ``tests/__init__.py`` can load the same declaration without
importing the Helm package and freezing unrelated process state.
"""

IDENTITY_PATH_ENV_KEYS = (
    "HELM_HOME", "MELD_HOME",
    "HELM_CHAT_DIR", "MELD_CHAT_DIR",
    "HELM_ACTORS", "MELD_ACTORS",
)
