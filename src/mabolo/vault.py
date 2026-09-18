"""The vault: a folder of Markdown in a Git repository, and nothing else.

Everything in here has to survive the tool being uninstalled, so the layout is
plain and the only files Mabolo needs are the ones the spec already defines.

The rules that make it safe to point this at a directory:

* **Every write is checked against the root**, right before it happens, and the
  path it checks is the path it writes. A check on a different spelling of the
  same name is not a check.
* **Symlinks are refused**, not followed. A vault that writes through a link
  writes wherever somebody else decided.
* **An area is a folder name**, never a path.
* **The index is derived**, so it is rebuilt from the entries, and it refuses to
  overwrite anything that looks like an entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter, git
from .errors import MaboloError
from .schema import (
    FIXED_AREAS,
    INDEX_FILE,
    LOG_FILE,
    OKF_VERSION,
    RESERVED_STEMS,
    Entry,
    area_to_dir,
    is_project_area,
    is_safe_area,
)
from .validate import Report, area_of, markdown_files, validate_meta, validate_vault

#: Everything derived lives here. Only the eval cases are versioned with the vault.
STATE_DIR = ".mabolo"
EVAL_DIR = f"{STATE_DIR}/eval"

VAULT_GITIGNORE = """\
# The Markdown files are the only source. Everything else under {state} is
# derived from them, or belongs to one machine, and can be deleted at any time.
{state}/*

# The exception: an eval case and the entries it queries have to move through
# history together, or bisecting the memory later is impossible.
!{state}/eval/
!{state}/eval/**

*.log
""".format(state=STATE_DIR)

LOG_HEADER = "<!-- Newest first, one heading per day, written by Mabolo. -->\n"

INDEX_INTRO = {
    "persona": "Who the person is, and how they want to be worked with.",
    "hosts": "One profile per machine, plus its topics.",
    "infra": "What holds across machines: network, services, accounts.",
    "design": "Taste: rules that override a model's defaults when a file is touched.",
    "project": "One folder per project.",
}

def escape_markdown(text: str) -> str:
    """Text that cannot break out of a Markdown link or add a line of its own."""
    one_line = " ".join(text.split())
    for char in ("\\", "[", "]", "(", ")"):
        one_line = one_line.replace(char, "\\" + char)
    return one_line


@dataclass
class Action:
    """One step `init` intends to take, printed before anything is written."""

    kind: str  # create | rewrite | exists | skip
    target: str
    note: str = ""

    def render(self, root: Path | None = None) -> str:
        """One line. Paths are shown inside the vault, so they fit a narrow screen."""
        note = f"  ({self.note})" if self.note else ""
        target = self.target
        if root is not None:
            try:
                relative = Path(target).relative_to(root)
                target = f"{root.name}/{relative}" if relative.parts else str(root)
            except ValueError:
                pass
        return f"  {self.kind:7} {target}{note}"


@dataclass
class Vault:
    """A vault on disk. Creating the object touches nothing."""

    root: Path
    areas: tuple[str, ...] = field(default=FIXED_AREAS)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()
        self.areas = tuple(self.areas)
        unusable = [a for a in self.areas if not is_safe_area(a)]
        if unusable:
            raise MaboloError(f"an area is one plain folder name, so {unusable} cannot be used")

    # Layout

    @property
    def index_file(self) -> Path:
        return self.root / INDEX_FILE

    @property
    def log_file(self) -> Path:
        return self.root / LOG_FILE

    @property
    def eval_dir(self) -> Path:
        return self.root / EVAL_DIR

    def area_dir(self, area: str) -> Path:
        if not (is_safe_area(area) or is_project_area(area)):
            raise MaboloError(f"{area!r} is not an area")
        return self.root / area_to_dir(area)

    def path_for(self, area: str, name: str) -> Path:
        target = frontmatter.normalise_target(self.area_dir(area) / f"{name}.md")
        self.ensure_inside(target)
        return target

    def exists(self) -> bool:
        return self.root.is_dir()

    def is_initialised(self) -> bool:
        return self.index_file.exists()

    def is_git_repository(self) -> bool:
        return (self.root / ".git").exists()

    # Containment

    def ensure_inside(self, path: Path) -> Path:
        """Refuse a path that leaves the vault or passes through a symlink.

        The check runs on the exact path that will be written, after the file
        name has been normalised, because a check on a different spelling is no
        check at all.
        """
        target = Path(path)
        root = self.root.resolve()
        try:
            target.resolve().relative_to(root)
        except (ValueError, OSError):
            raise MaboloError(f"{target} is outside the vault") from None
        # Lexical check as well: a symlink could make resolve() agree today and
        # something else tomorrow.
        try:
            target.absolute().relative_to(self.root.absolute())
        except ValueError:
            raise MaboloError(f"{target} is outside the vault") from None
        current = target
        while True:
            if current.is_symlink():
                raise MaboloError(f"{current} is a symlink, and the vault does not write through those")
            if current == self.root or current.parent == current:
                break
            current = current.parent
        return target

    # Reading

    def entry_paths(self) -> list[Path]:
        return [p for p in markdown_files(self.root) if p.name not in (INDEX_FILE, LOG_FILE)]

    def read_entry(self, path: Path) -> Entry:
        doc = frontmatter.read(path)
        if doc.meta is None:
            raise MaboloError(f"{path} has no frontmatter, so it is not an entry")
        return Entry.from_meta(doc.meta, body=doc.body, path=path, revision=doc.revision)

    def entries(self) -> list[Entry]:
        out = []
        for path in self.entry_paths():
            try:
                out.append(self.read_entry(path))
            except (MaboloError, OSError, UnicodeDecodeError):
                continue  # named by `validate`, never silently repaired
        return out

    def validate(self) -> Report:
        return validate_vault(self.root, self.areas)

    # Writing

    def write_entry(self, entry: Entry, path: Path | None = None, *, allow_findings: bool = False) -> Path:
        """Write one entry. Committing it is a separate concern.

        An entry with errors is refused unless the caller says it knows. Writing
        a whole file back is how a read, a small change and a write turn into a
        silent repair of everything else in that file. A caller that converts
        entries and reports every finding itself passes `allow_findings`.
        """
        target = Path(path) if path else self.path_for(entry.area, entry.name)
        target = frontmatter.normalise_target(target)
        if target.stem in RESERVED_STEMS:
            raise MaboloError(
                f"{target.name} is a reserved name: the index and the log are rebuilt from the entries"
            )
        self.ensure_inside(target)
        meta = entry.to_meta()
        if not allow_findings:
            problems = [
                p
                for p in validate_meta(meta, path=target, body=entry.body, areas=self.areas)
                if p.is_error
            ]
            if problems:
                raise MaboloError(
                    f"{target.name} has {len(problems)} error(s) and was not written: "
                    + "; ".join(p.message for p in problems[:3])
                )
        frontmatter.write(target, meta, entry.body)
        return target

    # Creating

    def plan(self) -> list[Action]:
        """What `init` would do, without doing any of it."""
        actions: list[Action] = []
        seen = self.root.is_dir()
        actions.append(Action("exists" if seen else "create", str(self.root), "vault root"))
        actions.append(
            Action(
                "exists" if self.is_git_repository() else "create",
                str(self.root / ".git"),
                "Git repository",
            )
        )
        for target, note in self._skeleton_targets():
            if not target.exists():
                actions.append(Action("create", str(target), note))
            elif target.name == INDEX_FILE:
                # An index is derived, so `init` rebuilds it. Saying "exists"
                # would hide that the file is about to be replaced.
                actions.append(Action("rewrite", str(target), f"{note}, rebuilt from the entries"))
            else:
                actions.append(Action("exists", str(target), note))
        return actions

    def _skeleton_targets(self) -> list[tuple[Path, str]]:
        targets = [
            (self.root / ".gitignore", "what is derived and disposable"),
            (self.index_file, f"bundle index, declares OKF {OKF_VERSION}"),
            (self.log_file, "journal, newest first"),
            (self.eval_dir, "eval cases, versioned with the entries"),
        ]
        for area in self.areas:
            targets.append((self.area_dir(area) / INDEX_FILE, f"area {area}"))
        targets.append((self.root / "project" / INDEX_FILE, "area project/<name>"))
        return targets

    def initialise(self) -> list[Action]:
        """Create what is missing. Running it twice changes nothing."""
        done: list[Action] = []
        created_root = not self.root.is_dir()
        if self.root.is_symlink():
            raise MaboloError(f"{self.root} is a symlink, and a vault is not created through one")
        self.root.mkdir(parents=True, exist_ok=True)
        done.append(Action("create" if created_root else "exists", str(self.root), "vault root"))

        gitignore = self.ensure_inside(self.root / ".gitignore")
        if not gitignore.exists():
            gitignore.write_text(VAULT_GITIGNORE, encoding="utf-8")
            done.append(Action("create", str(gitignore)))
        else:
            done.append(Action("exists", str(gitignore)))

        eval_dir = self.ensure_inside(self.eval_dir)
        if not eval_dir.exists():
            eval_dir.mkdir(parents=True, exist_ok=True)
            (eval_dir / ".gitkeep").write_text("", encoding="utf-8")
            done.append(Action("create", str(eval_dir)))
        else:
            done.append(Action("exists", str(eval_dir)))

        log_file = self.ensure_inside(self.log_file)
        if not log_file.exists():
            log_file.write_text(LOG_HEADER, encoding="utf-8")
            done.append(Action("create", str(log_file)))
        else:
            done.append(Action("exists", str(log_file)))

        for area in (*self.areas, "project"):
            directory = self.ensure_inside(self.area_dir(area))
            existed = (directory / INDEX_FILE).exists()
            directory.mkdir(parents=True, exist_ok=True)
            done.append(Action("exists" if existed else "create", str(directory)))

        for written in self.rebuild_indexes():
            done.append(Action("rewrite", str(written), "derived from the entries"))
        return done

    # The bundle index, which the spec reserves and defines

    def rebuild_indexes(self) -> list[Path]:
        """Write `index.md` for the root and every folder that holds entries."""
        written: list[Path] = []
        directories = {self.root}
        for path in self.entry_paths():
            directories.add(path.parent)
        for area in (*self.areas, "project"):
            directory = self.area_dir(area)
            if directory.is_dir():
                directories.add(directory)
        # Deepest first: a folder index has to exist before the index above it
        # counts what is in it.
        for directory in sorted(directories, key=lambda d: (-len(d.parts), str(d))):
            written.append(self._write_index(directory))
        return list(reversed(written))

    def _write_index(self, directory: Path) -> Path:
        is_root = directory == self.root
        target = self.ensure_inside(directory / INDEX_FILE)
        self._refuse_to_overwrite_an_entry(target)
        entries = sorted(
            (p for p in directory.glob("*.md") if p.name not in (INDEX_FILE, LOG_FILE)),
            key=lambda p: p.name,
        )
        subdirs = sorted(
            d
            for d in directory.iterdir()
            if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and any(d.rglob("*.md"))
        )
        relative = directory.relative_to(self.root)
        # A fixed heading at the root: naming it after the folder would make the
        # file differ between two machines that cloned the vault to a different
        # path, which is a diff that means nothing.
        heading = "Vault" if is_root else str(relative)
        lines = [f"# {heading}", ""]
        intro = INDEX_INTRO.get(relative.parts[0] if relative.parts else "", "")
        if is_root:
            intro = "Every entry in this vault, grouped by area."
        if intro:
            lines += [intro, ""]

        if entries:
            lines += ["## Entries", ""]
            lines += [self._index_line(path) for path in entries]
            lines.append("")
        if subdirs:
            lines.append("## Areas" if is_root else "## Folders")
            lines.append("")
            for sub in subdirs:
                count = len([p for p in sub.rglob("*.md") if p.name not in (INDEX_FILE, LOG_FILE)])
                lines.append(f"* [{escape_markdown(sub.name)}]({sub.name}/{INDEX_FILE}) - {count} entries")
            lines.append("")
        if not entries and not subdirs:
            lines += ["Nothing here yet.", ""]

        meta = {"okf_version": OKF_VERSION} if is_root else None
        frontmatter.write(target, meta, "\n".join(lines))
        return target

    def _refuse_to_overwrite_an_entry(self, target: Path) -> None:
        """An index is generated. If a file of that name carries a type, it is an entry."""
        if not target.exists():
            return
        try:
            doc = frontmatter.read(target)
        except (MaboloError, OSError, UnicodeDecodeError):
            return  # unreadable, and `validate` is the place that says so
        if doc.meta and doc.meta.get("type"):
            raise MaboloError(
                f"{target} looks like an entry, not a generated index. "
                "Rename it before Mabolo rebuilds the index here."
            )

    def _index_line(self, path: Path) -> str:
        try:
            doc = frontmatter.read(path)
        except (MaboloError, OSError, UnicodeDecodeError):
            return f"* [{escape_markdown(path.stem)}]({path.name}) - unreadable frontmatter"
        meta = doc.meta or {}
        raw_title = meta.get("title")
        title = escape_markdown(str(raw_title)) if raw_title else escape_markdown(path.stem)
        raw_description = meta.get("description")
        description = escape_markdown(str(raw_description)) if raw_description else ""
        return f"* [{title}]({path.name}) - {description}" if description else f"* [{title}]({path.name})"

    # Git

    def git(self, *args: str, check: bool = True):
        return git.run(self.root, *args, check=check)

    def git_available(self) -> bool:
        return git.available()

    def git_identity_missing(self) -> bool:
        return git.identity_missing(self.root)

    def git_initialise(self, branch: str = "main", paths: list[Path] | None = None) -> str:
        """Create the repository and commit exactly the files that were created."""
        if not git.available():
            return "git is not installed, so the vault is a plain folder for now"
        if not self.is_git_repository():
            self.git("init", "-b", branch)
        if self.git_identity_missing():
            return (
                "created the repository but made no commit: git has no user.email set. "
                "Set it, then commit the vault yourself."
            )
        wanted = paths if paths is not None else self._skeleton_files()
        relative = []
        for path in wanted:
            try:
                relative.append(str(Path(path).relative_to(self.root)))
            except ValueError:
                continue
        if not relative:
            return "nothing to commit"
        self.git("add", "--", *relative)
        staged = self.git("diff", "--cached", "--name-only", check=False)
        if not staged.stdout.strip():
            return "nothing to commit, the vault is already in Git"
        self.git("commit", "-m", "Create vault")
        head = self.git("rev-parse", "--short", "HEAD", check=False).stdout.strip()
        return f"committed the vault skeleton as {head}"

    def _skeleton_files(self) -> list[Path]:
        """The files `init` itself creates, named one by one rather than by folder."""
        files = [self.root / ".gitignore", self.index_file, self.log_file, self.eval_dir / ".gitkeep"]
        for area in (*self.areas, "project"):
            files.append(self.area_dir(area) / INDEX_FILE)
        return [f for f in files if f.exists()]

    def git_set_remote(self, url: str, name: str = "origin") -> str:
        if not self.is_git_repository():
            raise MaboloError("the vault is not a Git repository yet")
        if url.startswith("-"):
            raise MaboloError("a remote URL cannot start with a dash")
        existing = self.git("remote", "get-url", "--", name, check=False)
        if existing.returncode == 0:
            current = existing.stdout.strip()
            if current == url:
                return f"remote {name} already points at {git.redact(url)}"
            self.git("remote", "set-url", "--", name, url)
            return f"remote {name} moved from {git.redact(current)} to {git.redact(url)}"
        self.git("remote", "add", "--", name, url)
        return f"remote {name} set to {git.redact(url)}"


def area_for_path(root: Path, path: Path) -> str | None:
    """The area a path belongs to, derived from its folder."""
    return area_of(root, path)
