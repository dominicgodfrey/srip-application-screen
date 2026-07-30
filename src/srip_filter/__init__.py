"""SRIP Track 2 application filtering & ranking system.

Transport-agnostic core: reject applications that fail deterministic hard gates, then score and
rank the survivors. The HTTP shell and the review UI are thin layers over this package.

See CLAUDE.md (how it is built) and SRIP_ATS_PRD_v3.md (what it decides).
"""

__version__ = "0.1.0"
