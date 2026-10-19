"""The promotion gate (spec 0015): pure, stdlib only, deterministic.

It turns a study's stored runs into a verdict (pass, fail, or insufficient evidence) with machine
readable reasons and metrics. It never reads a clock or unseeded randomness, and imports nothing
outside the standard library and this package (AC-23, enforced by ``tests/test_core_boundary.py``).
"""
