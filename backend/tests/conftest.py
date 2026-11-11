"""Pytest configuration for the Connect ACGR Resource Replicator backend tests.

Fixes the pydantic import issue caused by deploy.sh bundling Linux x86_64
pydantic binaries into the backend directory. When running tests locally on
macOS, those Linux .so files can't be loaded. This conftest ensures the
system-installed pydantic takes precedence over the bundled one.
"""

import importlib
import sys


def _fix_pydantic_imports():
    """Ensure system pydantic is used instead of the bundled Linux binaries."""
    backend_dir = str(__import__("pathlib").Path(__file__).resolve().parent.parent)

    # Find system site-packages pydantic
    import site
    site_packages = site.getsitepackages()

    # Remove any already-imported pydantic modules so we can re-import from system
    pydantic_modules = [k for k in sys.modules if k.startswith(("pydantic", "pydantic_core"))]
    for mod in pydantic_modules:
        del sys.modules[mod]

    # Insert system site-packages at the front of sys.path (before backend dir)
    for sp in reversed(site_packages):
        if sp not in sys.path:
            sys.path.insert(0, sp)
        elif sys.path.index(sp) > sys.path.index(backend_dir) if backend_dir in sys.path else False:
            sys.path.remove(sp)
            sys.path.insert(0, sp)

    # Verify pydantic can be imported
    try:
        import pydantic
        import pydantic_core
    except ImportError:
        pass  # Will fail later with a clearer error


_fix_pydantic_imports()
