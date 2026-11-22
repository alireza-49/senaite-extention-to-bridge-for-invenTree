"""SENAITE -> InvenTree bridge.

Polls SENAITE for verified analyses and deducts the consumables configured for
each analysis keyword from InvenTree stock, and reports InvenTree parts that
dropped below their minimum stock level.
"""

__version__ = "1.0.0"
