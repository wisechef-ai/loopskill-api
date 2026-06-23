"""Vendored reconcile library for the recipes-cookbook-reconcile skill.

This package is bundled INSIDE the skill (beside recipes-reconcile) so the
client is fully self-contained — the host imports these modules directly with
no pip install. See reconcile_client.py for the atomic apply + rollback engine.
"""
