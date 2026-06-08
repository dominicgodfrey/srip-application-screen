"""SRIP Track 2 application filtering & ranking system.

Stateless core: reject applications that fail deterministic hard gates, then score and rank
the survivors. The API and any future UI are thin shells over this package.

See CLAUDE.md (how it is built) and SRIP_Application_Filter_PRD.md (what it decides).
"""

__version__ = "0.1.0"
