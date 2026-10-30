"""Reconciliation (spec 0023): the trader's orders and positions against the exchange's, as pure values.

``views`` holds the frozen ``ExchangeView`` and ``LocalView`` and the ``ExchangePort`` an adapter
fills; ``compare`` turns the two views into findings. No I/O and no Nautilus import: the
``ReconcileActor`` in ``trading_core.node.reconcile`` fetches, reads the cache, and acts.
"""
