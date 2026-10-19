"""Frozen strategy versions (spec 0015 AC-1): a hash of a strategy's own code, config, and engine.

Not pure: it reads the installed ``trading_core`` source files. It never imports them. A strategy
lives in a ``trading_core.node.<module>`` module (its class's ``__module__``; the paper strategy
``placeholder_timed`` lives in ``trading_core.node.placeholder``). Its closure is that module plus every
``trading_core`` module it imports, transitively, found by parsing each file with ``ast``. Every
``import`` and ``from … import`` node counts, wherever it sits (inside a function too), and relative
imports resolve against the importing module's package. Importing a module runs its packages'
``__init__`` files, so those count too. Third party and stdlib modules are left out.

``code_hash`` is the sha256 over the sorted ``(module path, source bytes)`` pairs, so two images built
from different commits with the same closure files give the same hash. ``frozen_hash`` adds the
config and the NautilusTrader version, because an engine upgrade changes fills. A closure module
that imports ``importlib`` or calls ``__import__`` would hide what it loads, so ``code_closure``
refuses it with ``DynamicImportError``.
"""

import ast
import hashlib
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Final

from trading_core.recording.rows import JsonObject, canonical_json

PACKAGE: Final = "trading_core"
STRATEGY_PACKAGE: Final = f"{PACKAGE}.node"
_DYNAMIC: Final = "importlib"


class VersioningError(Exception):
    """Base for every error raised while freezing a strategy version."""


class DynamicImportError(VersioningError):
    """A closure module imports ``importlib`` or calls ``__import__``, so its closure cannot be known."""


class UnknownStrategyModuleError(VersioningError):
    """The strategy module is not a ``trading_core.node`` module with a source file."""


@dataclass(frozen=True)
class FrozenHashes:
    """The two hashes ``ensure_strategy_version`` stores on a version."""

    code_hash: str
    frozen_hash: str


def package_root() -> Path:
    """The installed ``trading_core`` folder (the parent of this file)."""
    return Path(__file__).resolve().parent


def _source_file(root: Path, module: str) -> Path | None:
    """The file of ``module`` (``trading_core.a.b``) under ``root``, a module or a package, else None."""
    parts = module.split(".")
    if parts[0] != PACKAGE:
        return None
    base = root.joinpath(*parts[1:])
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _package_of(module: str, file: Path) -> str:
    """The package a relative import in ``module`` resolves against."""
    return module if file.name == "__init__.py" else module.rpartition(".")[0]


def _with_parents(module: str) -> list[str]:
    """``a.b.c`` and the packages importing it runs: ``a``, ``a.b``, ``a.b.c``."""
    parts = module.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def _imports(module: str, file: Path, tree: ast.AST) -> list[str]:
    """Every module name an ``import`` node in ``tree`` may load, ``from x import y`` giving ``x`` and ``x.y``.

    Raises ``DynamicImportError`` for ``importlib`` or a call to ``__import__``.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == _DYNAMIC:
                    raise DynamicImportError(f"{module} imports {alias.name}")
                found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = _package_of(module, file).split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, node.module] if node.module else anchor)
            else:
                base = node.module or ""
            if base.split(".")[0] == _DYNAMIC:
                raise DynamicImportError(f"{module} imports from {base}")
            found.append(base)
            found.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.Name) and node.id == "__import__":
            raise DynamicImportError(f"{module} uses __import__")
    return found


def code_closure(module: str, root: Path | None = None) -> list[tuple[str, bytes]]:
    """The sorted ``(module path, source bytes)`` pairs of a strategy module and its ``trading_core`` imports.

    ``module`` is dotted (``trading_core.node.macd_baseline``). The path is relative to the folder
    holding ``trading_core`` (``trading_core/node/x.py``), so it is the same on every machine. Raises
    ``UnknownStrategyModuleError`` when ``module`` is not a ``trading_core.node`` module with a source
    file, and ``DynamicImportError`` as ``_imports`` says.
    """
    root = package_root() if root is None else root
    start = module
    if not start.startswith(f"{STRATEGY_PACKAGE}.") or _source_file(root, start) is None:
        raise UnknownStrategyModuleError(f"{start} is not a {STRATEGY_PACKAGE} module with source under {root}")
    seen: dict[str, Path] = {}
    pending = _with_parents(start)
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        file = _source_file(root, module)
        if file is None:
            continue  # an attribute imported from a package, or a module outside trading_core
        seen[module] = file
        source = file.read_bytes()
        for name in _imports(module, file, ast.parse(source, filename=str(file))):
            if name.split(".")[0] == PACKAGE:
                pending.extend(_with_parents(name))
    return sorted((file.relative_to(root.parent).as_posix(), file.read_bytes()) for file in seen.values())


def code_hash(closure: list[tuple[str, bytes]]) -> str:
    """sha256 hex over the sorted closure: each path, a NUL, the sha256 of its source, and a newline."""
    digest = hashlib.sha256()
    for path, source in sorted(closure):
        digest.update(path.encode() + b"\0" + hashlib.sha256(source).hexdigest().encode() + b"\n")
    return digest.hexdigest()


def frozen_hash(code: str, config: JsonObject, nautilus_version: str) -> str:
    """sha256 hex of the canonical JSON of ``{code_hash, config, nautilus_version}`` (AC-1)."""
    payload = canonical_json({"code_hash": code, "config": config, "nautilus_version": nautilus_version})
    return hashlib.sha256(payload.encode()).hexdigest()


def installed_nautilus_version() -> str:
    """The installed NautilusTrader version, read from package metadata (nothing is imported)."""
    return version("nautilus_trader")


def freeze(module: str, config: JsonObject, nautilus_version: str | None = None) -> FrozenHashes:
    """Both hashes of the strategy in ``module`` with ``config``, over the installed source and NautilusTrader."""
    code = code_hash(code_closure(module))
    engine = installed_nautilus_version() if nautilus_version is None else nautilus_version
    return FrozenHashes(code_hash=code, frozen_hash=frozen_hash(code, config, engine))
