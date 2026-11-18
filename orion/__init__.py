"""Orion - lightweight self-hosted VPS hosting panel."""

# Bumped with the theme, not with every commit: `base_*.html` link the
# stylesheet as `?v={{ asset_version }}`, so this number is what tells a
# returning browser to fetch the new sheet. A theme change that does not move it
# is a theme change nobody sees.
__version__ = "0.3.2"
