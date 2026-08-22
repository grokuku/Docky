"""Sous-package de l'agent : modules cohésifs extraits de docker_manager.

Chaque module ci-dessous contient un domaine isolé (validation/chemins,
update-check, streaming compose, git, import, ports, events) et est ré-exporté
dans le namespace ``agent.docker_manager`` (façade) pour préserver les imports
de routes.py/main.py et les monkeypatchs des tests.
"""
