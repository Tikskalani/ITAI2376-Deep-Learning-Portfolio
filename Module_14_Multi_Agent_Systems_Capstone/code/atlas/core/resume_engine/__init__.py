"""The APEX evidence engine, ported unmodified (see PROVENANCE.md).

apex_engine.py and apex_domains.py are byte-for-byte copies of APEX at the
apex-v11-final tag. The engine does a top-level `import apex_domains`, so this
package registers its copy under that name before importing the engine. That
keeps both files untouched; Atlas code imports them from here:

    from core.resume_engine import apex_engine, apex_domains
"""

from __future__ import annotations

import sys

from core.resume_engine import apex_domains

_existing = sys.modules.get("apex_domains")
if _existing is not None and _existing is not apex_domains:
    raise ImportError("another module is already registered as apex_domains")
sys.modules["apex_domains"] = apex_domains

from core.resume_engine import apex_engine  # noqa: E402  (needs the alias above)

__all__ = ["apex_domains", "apex_engine"]
