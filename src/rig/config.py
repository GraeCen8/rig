"""Manifest parsing, root resolution, and package list aggregation."""

from __future__ import annotations

import os
import platform
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit
from tomlkit.items import Null

ROOT_DEFAULT = Path("~/dots").expanduser()


class ConfigError(Exception):
    """Raised when the manifest or directory layout is invalid."""


def host_os() -> str:
    """Return the current OS name in lowercase (e.g. ``'linux'``, ``'darwin'``)."""
    return platform.system().lower()


@dataclass
class SystemEntry:
    """A single ``[[system]]`` block declaring packages, modules, and filters."""

    name: str
    packages: list[str]
    modules: list[str] = field(default_factory=list)
    os: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)

    def applies(self, active_profiles: list[str] | None = None) -> bool:
        """Return True if this entry matches the current OS and profiles."""
        if self.os and host_os() not in {o.lower() for o in self.os}:
            return False
        return not (self.profiles and not (set(active_profiles or []) & set(self.profiles)))


@dataclass
class Root:
    """Parsed representation of a root ``rig.toml`` and its settings."""

    dir: Path
    manager: str | None
    modules: list[str]
    stow: list[str]
    mode: str
    systems: list[SystemEntry]
    post_hooks: list[str]
    git_url: str | None = None
    template_vars: dict = field(default_factory=dict)
    profiles: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    git_autocommit: bool = False
    top: bool = True
    pkgs_map: dict = field(default_factory=dict)

    @classmethod
    def load(cls, root_dir: Path, top: bool = True) -> Root:
        """Load and parse a ``rig.toml`` from *root_dir*.

        Args:
            root_dir: Directory containing ``rig.toml``.
            top: Whether this is the top-level root (only the top root
                may declare ``[manager]``).

        Returns:
            A populated Root dataclass.

        Raises:
            ConfigError: If the manifest is missing or has invalid fields.
        """
        rig_toml = root_dir / "rig.toml"
        if not rig_toml.is_file():
            raise ConfigError(f"{root_dir}: no rig.toml found")
        with open(rig_toml, "rb") as fh:
            data = tomllib.load(fh)
        manager = (data.get("manager") or {}).get("name") or None
        cfg = data.get("config") or {}
        stow = [s for s in cfg.get("stow", [])]
        mode = cfg.get("mode", "rig")
        if mode not in ("rig", "stow"):
            raise ConfigError(f"{rig_toml}: config.mode must be 'rig' or 'stow', got {mode!r}")
        template_vars = dict(cfg.get("vars", {}))
        systems = [
            SystemEntry(
                name=e.get("name", "default"),
                packages=[p for p in e.get("packages", [])],
                modules=[m for m in e.get("modules", [])],
                os=[o for o in e.get("os", [])],
                profiles=[p for p in e.get("profiles", [])],
            )
            for e in data.get("system", [])
        ]
        hooks = [h for h in (data.get("hooks") or {}).get("post", [])]
        git = data.get("git") or {}
        git_url = git.get("url") or None
        git_autocommit = bool(git.get("autocommit"))
        pkgs_map = data.get("pkgs_map", {})
        return cls(
            dir=root_dir.resolve(),
            manager=manager,
            modules=[],
            stow=stow,
            mode=mode,
            systems=systems,
            post_hooks=hooks,
            git_url=git_url,
            template_vars=template_vars,
            profiles=list(cfg.get("profiles", [])),
            ignore=list(cfg.get("ignore", [])),
            git_autocommit=git_autocommit,
            top=top,
            pkgs_map=pkgs_map,
        )


def _active_modules(root: Root) -> list[str]:
    """Return the deduplicated list of module names enabled by applicable system blocks."""
    seen: set[str] = set()
    result: list[str] = []
    for entry in root.systems:
        if entry.applies(root.profiles):
            for m in entry.modules:
                if m not in seen:
                    seen.add(m)
                    result.append(m)
    return result


def roots_in_order(root_dir: Path) -> list[Root]:
    """Return all roots (including modules) in depth-first apply order.

    Modules are visited before their parent, so the returned list is safe
    to iterate in order for syncing.

    Args:
        root_dir: Path to the top-level root directory.

    Returns:
        List of Root objects in depth-first, modules-before-parent order.

    Raises:
        ConfigError: If a declared module directory or its ``rig.toml`` is missing.
    """
    order: list[Root] = []

    def visit(d: Path, top: bool) -> None:
        root = Root.load(d, top)
        root.modules = _active_modules(root)
        if not root.top and root.manager:
            print(
                f"warning: {root.dir}/rig.toml: [manager] applies only at the top root; ignored",
                file=sys.stderr,
            )
        for name in root.modules:
            module_dir = d / name
            if not module_dir.is_dir():
                raise ConfigError(f"{d}: enabled module {name!r} not found")
            visit(module_dir, top=False)
        order.append(root)

    visit(root_dir, top=True)
    return order


def resolve_package_name(pkg: str, manager: str, pkgs_map: dict) -> str:
    """Map a package name through ``[pkgs_map]`` for the given manager.

    If *pkg* has a mapping for *manager* in *pkgs_map*, return the
    mapped name; otherwise return *pkg* unchanged.
    """
    if pkg in pkgs_map:
        mapped = pkgs_map[pkg]
        if isinstance(mapped, dict) and manager in mapped:
            return mapped[manager]
    return pkg


def all_packages(root_dir: Path, manager: str | None = None) -> list[str]:
    """Collect the deduplicated, ordered list of all packages across all roots.

    Applies OS and profile filters, resolves ``[pkgs_map]`` mappings, and
    preserves insertion order.

    Args:
        root_dir: Path to the top-level root directory.
        manager: Package manager name for mapping. Auto-detected if None.

    Returns:
        List of unique package names in declaration order.
    """
    active = Root.load(root_dir, top=True).profiles
    roots = roots_in_order(root_dir)
    # find package manager (string) if not given
    if manager is None:
        from rig import pms as pms_mod

        mgr = pms_mod.detect(Root.load(root_dir, top=True).manager)
        manager = mgr.name
    packages: list[str] = []
    for root in roots:
        pkgs_map = root.pkgs_map if hasattr(root, "pkgs_map") else {}
        for entry in root.systems:
            if entry.applies(active):
                for pkg in entry.packages:
                    mapped_pkg = resolve_package_name(pkg, manager, pkgs_map)
                    if mapped_pkg not in packages:
                        packages.append(mapped_pkg)
    return packages


def _unfiltered_block_index(data: dict) -> int | None:
    """Return the index of the first ``[[system]]`` block with no OS or profile filters."""
    blocks = data.get("system")
    if not isinstance(blocks, list):
        return None
    for i, block in enumerate(blocks):
        if isinstance(block, dict) and "os" not in block and "profiles" not in block:
            return i
    return None


def _os_matches(values: list[str]) -> bool:
    """Return True if any value in *values* matches the current OS."""
    h = host_os()
    return any(h == v.lower() for v in values)


def _profiles_match(block_profiles: list[str], active: list[str]) -> bool:
    """Return True if *block_profiles* overlaps with *active* profiles (or block has none)."""
    if not block_profiles:
        return True
    return bool(set(active) & set(block_profiles))


def _remove_block_index(data: dict, packages: list[str], active_profiles: list[str]) -> int | None:
    """Return the index of the first ``[[system]]`` block containing any of *packages*."""
    blocks = data.get("system")
    if not isinstance(blocks, list):
        return None
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        hits = any(p in block.get("packages", []) for p in packages)
        os_ok = not block.get("os") or _os_matches(block["os"])
        profiles_ok = _profiles_match(list(block.get("profiles", [])), active_profiles)
        if hits and os_ok and profiles_ok:
            return i
    return None


def _parse_document(rig_toml: Path) -> tomlkit.TOMLDocument:
    """Parse *rig_toml* into a lossless tomlkit document.

    Unlike a plain ``tomllib`` load, the returned document keeps comments,
    inline comments, whitespace and key ordering, so editing it and dumping
    it back preserves the user's formatting.

    Raises:
        ConfigError: If the manifest cannot be read or is invalid TOML.
    """
    try:
        text = rig_toml.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"{rig_toml}: cannot read manifest: {e}") from e
    try:
        return tomlkit.parse(text)
    except tomlkit.exceptions.ParseError as e:
        raise ConfigError(f"{rig_toml}: invalid TOML: {e}") from e


def _harvest_array_trivia(
    arr: tomlkit.items.Array | None,
) -> tuple[list[tuple[str, list[str], str | None]], list[str]]:
    """Extract comment trivia from a tomlkit array.

    Returns ``(items, trailing)`` where *items* is a list of
    ``(name, comments_before, inline_comment)`` tuples in declaration order
    and *trailing* holds comments after the last item (before ``]``).
    """
    items: list[tuple[str, list[str], str | None]] = []
    pending: list[str] = []
    for group in getattr(arr, "_value", None) or []:
        comment = str(group.comment).strip() if group.comment else None
        value = group.value
        if value is None or isinstance(value, Null):
            # A standalone comment line, or the whitespace-only final group.
            if comment:
                pending.append(comment)
            continue
        items.append((str(value), list(pending), comment))
        pending = []
    return items, pending


def _carry_comments(
    items: list[tuple[str, list[str], str | None]], surviving: set[str]
) -> tuple[list[tuple[str, list[str], str | None]], list[str]]:
    """Re-map comment trivia so no comment is lost when items are removed.

    Standalone comments attached to removed items are carried forward to the
    next surviving item; inline comments are tied to their package and are
    dropped with it. Returns the surviving items and any comments left over.
    """
    out: list[tuple[str, list[str], str | None]] = []
    pending: list[str] = []
    for name, before, inline in items:
        pending.extend(before)
        if name not in surviving:
            continue
        out.append((name, pending, inline))
        pending = []
    return out, pending


def _format_packages_value(
    names: list[str],
    before: dict[str, list[str]],
    inline: dict[str, str],
    trailing: list[str],
) -> str:
    """Render a package list as a TOML array fragment (without ``packages =``).

    Single-line for at most 3 items when no comments need preserving,
    multi-line otherwise, with comments kept beside their packages.
    """
    has_comments = any(before.get(n) for n in names) or bool(inline) or bool(trailing)
    if len(names) <= 3 and not has_comments:
        return "[" + ", ".join(tomlkit.string(n).as_string() for n in names) + "]"

    lines: list[str] = []
    for name in names:
        lines.extend(f"    {c}" for c in before.get(name, ()))
        row = f"    {tomlkit.string(name).as_string()},"
        if name in inline:
            row += f"  {inline[name]}"
        lines.append(row)
    lines.extend(f"    {c}" for c in trailing)
    return "[\n" + "\n".join(lines) + "\n]"


def _packages_array_item(
    names: list[str],
    before: dict[str, list[str]],
    inline: dict[str, str],
    trailing: list[str],
) -> tomlkit.items.Array:
    """Build a native tomlkit Array with *names* and positioned comments.

    The rendered fragment is parsed back with tomlkit so the array carries
    exactly the trivia (indents, commas, comments) it will be written with.
    """
    payload = _format_packages_value(names, before, inline, trailing)
    return tomlkit.parse(f"packages = {payload}\n")["packages"]


def _new_system_block(names: list[str]) -> tomlkit.items.Table:
    """Build a fresh ``[[system]]`` table named ``base`` with *names*."""
    table = tomlkit.table()
    table["name"] = "base"
    table["packages"] = _packages_array_item(names, {}, {}, [])
    return table


def _atomic_write(rig_toml: Path, content: str) -> None:
    """Replace *rig_toml* with *content* atomically.

    Writes to a temp file in the same directory, fsyncs it, preserves the
    original file mode, then renames over the manifest so a crash can never
    leave a truncated or half-written ``rig.toml``.
    """
    fd, tmp_name = tempfile.mkstemp(dir=rig_toml.parent, prefix=f".{rig_toml.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, stat.S_IMODE(rig_toml.stat().st_mode))
        os.replace(tmp, rig_toml)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _edit_packages(rig_toml: Path, packages: list[str], add: bool) -> tuple[list[str], list[str]]:
    """Add or remove *packages* in *rig_toml*, preserving comments and style.

    The file is parsed once with tomlkit and dumped back losslessly, so
    unrelated formatting (headers with trailing comments, blank lines,
    key alignment, other sections) survives untouched. The result is
    re-validated with ``tomllib`` and written atomically.
    """
    doc = _parse_document(rig_toml)

    active_profiles = list((doc.get("config") or {}).get("profiles", []))
    idx = (
        _unfiltered_block_index(doc) if add else _remove_block_index(doc, packages, active_profiles)
    )
    blocks = doc.get("system")
    current = [str(p) for p in blocks[idx].get("packages", [])] if idx is not None else []
    if add:
        new = sorted(set(current) | set(packages))
    else:
        new = sorted(set(current) - set(packages))
    if new == current:
        return current, new

    if idx is None:
        if not add:
            return current, new
        if blocks is not None and not isinstance(blocks, list):
            raise ConfigError(
                f"{rig_toml}: 'system' must be an array of tables ([[system]]) to add packages"
            )
        block = _new_system_block(new)
        if isinstance(blocks, list):
            blocks.append(block)
        else:
            aot = tomlkit.aot()
            aot.append(block)
            doc["system"] = aot
    else:
        block = blocks[idx]
        arr = block.get("packages")
        items, trailing = _harvest_array_trivia(arr) if arr is not None else ([], [])
        survivors, carried = _carry_comments(items, set(new))
        before: dict[str, list[str]] = {}
        inline: dict[str, str] = {}
        for name, cm_before, cm_inline in survivors:
            before[name] = cm_before
            if cm_inline:
                inline[name] = cm_inline
        block["packages"] = _packages_array_item(new, before, inline, trailing + carried)

    out = tomlkit.dumps(doc)
    try:
        tomllib.loads(out)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{rig_toml}: refusing to write invalid TOML: {e}") from e
    _atomic_write(rig_toml, out)
    return current, new


def add_packages(rig_toml: Path, packages: list[str]) -> tuple[list[str], list[str]]:
    """Add *packages* to the unfiltered ``[[system]]`` block in *rig_toml*.

    Returns:
        Tuple of (before, after) package lists for the affected block.
    """
    return _edit_packages(rig_toml, packages, add=True)


def remove_packages(rig_toml: Path, packages: list[str]) -> tuple[list[str], list[str]]:
    """Remove *packages* from the matching ``[[system]]`` block in *rig_toml*.

    Returns:
        Tuple of (before, after) package lists for the affected block.
    """
    return _edit_packages(rig_toml, packages, add=False)
