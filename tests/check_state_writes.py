"""Register filesystem write calls in production Python; import aliases are not tracked."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    path: str
    qualname: str
    func_text: str
    line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return self.path, self.qualname, self.func_text


# (path, qualified function, call text, count, category, concrete reason)
ALLOWLIST: list[tuple[str, str, str, int, str, str]] = [
    ("clawchat_gateway/state_lock.py", "state_file_lock", "os.open", 1, "C",
     "C: state lock file"),
    ("clawchat_gateway/install_identity.py", "_publish_seed", "os.fdopen", 1, "C",
     "C: same-directory seed staging file, flushed and fsynced before create-only publication"),
    ("clawchat_gateway/install_identity.py", "_publish_seed", "tempfile.mkstemp", 1, "C",
     "C: same-directory seed staging file created for the create-only link publish"),
    ("clawchat_gateway/install_identity.py", "_publish_seed", "os.link", 1, "B",
     "B: create-only link publish + fsync_dir, never replaces a concurrent seed"),
    ("clawchat_gateway/install_identity.py", "_state_guard", "os.open", 1, "C",
     "C: installation state lock file"),
    ("clawchat_gateway/instance_lock.py", "acquire", "os.open", 1, "C",
     "C: instance lock file"),
    ("clawchat_gateway/instance_lock.py", "acquire", "os.fdopen", 1, "C",
     "C: lock holder diagnostics; ownership comes from the kernel lock"),
    ("clawchat_gateway/liveware_cli.py", "_stream_download", "os.fdopen", 1, "C",
     "C: binary staging file, verified and fsynced before publication"),
    ("clawchat_gateway/liveware_cli.py", "_download", "tempfile.mkstemp", 1, "C",
     "C: same-directory binary staging file, verified and fsynced before publication"),
    ("clawchat_gateway/liveware_cli.py", "_download", "os.replace", 1, "B",
     "B: verified liveware binary; file fsync after chmod, then fsync_dir"),
    ("clawchat_gateway/liveware_cli.py", "ensure_liveware_cli", "os.open", 1, "C",
     "C: liveware installation lock file"),
    ("clawchat_gateway/liveware_sample.py", "_write_synced", "path.open", 1, "C",
     "C: sample staging files, flushed and fsynced including preserved user data"),
    ("clawchat_gateway/liveware_sample.py", "download_liveware_sample", "app_dir.replace", 1, "B",
     "B: retain old app for rollback before publishing synced staging directory"),
    ("clawchat_gateway/liveware_sample.py", "download_liveware_sample", "tmp_dir.replace", 1, "B",
     "B: publish synced sample directory, fsync_dir on parent, roll back on swap failure"),
    ("clawchat_gateway/liveware_sample.py", "download_liveware_sample", "old_dir.replace", 2, "B",
     "B: restore the retained old app after a failed swap or a crash between the swap renames"),
    ("clawchat_gateway/llm_context_debug.py", "ClawChatLlmContextDebug.write_snapshot", "file_path.write_text", 1, "C",
     "C: debug snapshot, never read for decisions"),
    ("clawchat_gateway/llm_context_debug.py", "ClawChatLlmContextDebug.write_snapshot", "(target_root / 'latest.json').write_text", 1, "C",
     "C: latest debug snapshot, never read for decisions"),
    ("clawchat_gateway/media_runtime.py", "_download_inbound_media_sync", "partial_path.open", 1, "C",
     "C: per-message inbound media .part, consumed once"),
    ("clawchat_gateway/media_runtime.py", "_download_inbound_media_sync", "partial_path.replace", 1, "C",
     "C: per-message inbound media publication, consumed once"),
    ("clawchat_gateway/restart.py", "schedule_gateway_restart", "open", 1, "C",
     "C: child restart log, never read for decisions"),
    ("clawchat_gateway/restart.py", "_restart_guard", "os.open", 1, "C",
     "C: persistent restart mutex inode; only the kernel lock grants ownership"),
    ("clawchat_gateway/storage.py", "ClawChatStore._relocate_legacy_db", "src.replace", 1, "C",
     "C (G2): SQLite file migration, database writes governed by G2"),
]

_WRITE_FLAGS = {"O_CREAT", "O_WRONLY", "O_RDWR", "O_TRUNC", "O_APPEND"}
_DIRECT = {
    "os.replace", "os.rename", "os.link", "os.symlink", "shutil.move",
    "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
    # Named temp files are written through the returned fd / object, so the
    # creation call is the only place the gate can see the write.
    "tempfile.mkstemp", "tempfile.NamedTemporaryFile", "mkstemp", "NamedTemporaryFile",
}


def _is_write(call: ast.Call) -> bool:
    func = ast.unparse(call.func)
    attr = call.func.attr if isinstance(call.func, ast.Attribute) else ""
    if func in _DIRECT or attr in {"write_text", "write_bytes"}:
        return True
    if attr in {"replace", "rename"} and len(call.args) == 1 and not call.keywords:
        return True
    if func == "os.open":
        flags = next((k.value for k in call.keywords if k.arg == "flags"),
                     call.args[1] if len(call.args) > 1 else None)
        return flags is not None and any(
            (isinstance(n, ast.Name) and n.id in _WRITE_FLAGS)
            or (isinstance(n, ast.Attribute) and n.attr in _WRITE_FLAGS)
            for n in ast.walk(flags)
        )
    if func in {"open", "os.fdopen"} or attr == "open":
        index = 1 if func in {"open", "os.fdopen"} else 0
        mode = next((k.value for k in call.keywords if k.arg == "mode"),
                    call.args[index] if len(call.args) > index else None)
        return (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                and any(c in mode.value for c in "wax+"))
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.findings: list[Finding] = []

    def _scoped(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = _scoped
    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        if _is_write(node):
            self.findings.append(Finding(
                self.path, ".".join(self.scope) or "<module>",
                ast.unparse(node.func), node.lineno,
            ))
        self.generic_visit(node)


def scan(root: Path) -> list[Finding]:
    root = Path(root)
    paths = list((root / "clawchat_gateway").rglob("*.py"))
    paths.extend(root / name for name in ("clawchat_cli.py", "__init__.py")
                 if (root / name).is_file())
    findings: list[Finding] = []
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        # The sole atomic writer implements the protected writes being enforced.
        if relative == "clawchat_gateway/atomic_file.py":
            continue
        visitor = _Visitor(relative)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=relative))
        findings.extend(visitor.findings)
    return findings


def check(root: Path, allowlist=ALLOWLIST) -> list[str]:
    findings = scan(root)
    actual = Counter(f.key for f in findings)
    registered = {}
    errors = []
    for path, qualname, func, count, category, reason in allowlist:
        key = path, qualname, func
        if key in registered or count < 1 or category not in {"B", "C"} or not reason.strip():
            errors.append(f"invalid allowlist entry: {path} {func} in {qualname}")
        registered[key] = count
        found = actual[key]
        if found < count:
            errors.append(f"stale allowlist entry: {path} {func} in {qualname}: expected {count}, found {found}")
        elif found > count:
            errors.append(f"state write count exceeded: {path} {func} in {qualname}: expected {count}, found {found}")
    for finding in findings:
        if finding.key not in registered:
            errors.append(
                f"unregistered state write: {finding.path}:{finding.line} {finding.func_text} "
                f"in {finding.qualname} — use clawchat_gateway.atomic_file or add an allowlist entry with a reason"
            )
    return errors


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    errors = check(args.root)
    if errors:
        print("\n".join(errors))
        return 1
    print(f"state-write gate: {len(scan(args.root))} findings, all registered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
