"""The vault: a folder of Markdown in a Git repository, and nothing else.

Everything in here has to survive the tool being uninstalled, so the layout is
plain and the only files Mabolo needs are the ones the spec already defines.

The rules that make it safe to point this at a directory:

* **Every write is checked against the root**, right before it happens, and the
  path it checks is the path it writes. A check on a different spelling of the
  same name is not a check.
* **Symlinks inside the vault are refused**, not followed. A vault that writes
  through a link writes wherever somebody else decided. The root itself may be a
  link: the person chose that path, `init` prints where it really leads before
  writing anything, and refusing it here while the command line resolved it one
  line earlier meant the rule held for the library and not for the tool.
* **An area is a folder name**, never a path.
* **The index is derived**, so it is rebuilt from the entries, and it only ever
  replaces a file it could have written itself.
* **The plan names everything that will be touched.** `init` prints it and then
  does exactly that, because a plan that leaves something out is not consent.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import frontmatter, git
from .errors import MaboloError
from .schema import (
    DEFAULT_LANGUAGE,
    FIXED_AREAS,
    INDEX_FILE,
    LOG_FILE,
    NAME_RE,
    OKF_VERSION,
    RESERVED_STEMS,
    Entry,
    area_to_dir,
    is_language,
    is_project_area,
    is_safe_area,
    normalise_name,
)
from .validate import Report, area_of, iter_markdown, markdown_files, validate_meta, validate_vault

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


@dataclass(frozen=True)
class Skeleton:
    """One file a fresh vault starts with. `content` is None for a derived index."""

    path: Path
    note: str
    content: str | None = None


@dataclass(frozen=True)
class GitResult:
    """What the Git step did, so a caller can tell success from a sentence."""

    completed: bool
    message: str
    revision: str | None = None


@dataclass
class Vault:
    """A vault on disk. Creating the object touches nothing."""

    root: Path
    areas: tuple[str, ...] = field(default=FIXED_AREAS)
    #: The language to declare when the root index is next written. None means
    #: "keep whatever the vault already declares", which is what every command
    #: but `init` wants: rebuilding an index must not change what the vault says
    #: about itself.
    language: str | None = None

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()
        self.areas = tuple(self.areas)
        if self.language is not None and not is_language(self.language):
            raise MaboloError(f"{self.language!r} is not a language code, use two or three letters")
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
        """The file an entry of this name belongs in.

        The name is checked before it is joined onto a folder. `../elsewhere/x`
        resolves to a path that is still inside the vault, so containment alone
        says yes to it; the name rule is what keeps an entry in its own area.
        """
        self.check_name(name)
        target = frontmatter.normalise_target(self.area_dir(area) / f"{normalise_name(name)}.md")
        self.ensure_inside(target)
        return target

    @staticmethod
    def check_name(name: str) -> str:
        """An entry name is a file stem, and nothing that could be a path."""
        stem = normalise_name(str(name))
        if not stem:
            raise MaboloError("an entry needs a name, an empty one would become a hidden file")
        if stem in RESERVED_STEMS:
            raise MaboloError(
                f"{stem!r} is reserved: the index and the log are rebuilt from the entries"
            )
        if not NAME_RE.match(stem):
            raise MaboloError(
                f"{name!r} is not a usable entry name, use lower case, digits, dot, dash, underscore"
            )
        return stem

    def is_initialised(self) -> bool:
        return self.index_file.exists()

    def declared_language(self) -> str:
        """The language this vault says its entries are written in.

        It lives in the root `index.md`, the one file the format already
        reserves for saying what this vault is, so it travels with the entries
        through Git. The search reads it, and so does the baseline: stop words
        and suffixes differ by language, so a vault whose language sat in a
        machine's configuration file would rank differently on two machines and
        no history could be replayed.
        """
        try:
            doc = frontmatter.read(self.index_file)
        except (MaboloError, OSError, UnicodeDecodeError):
            return DEFAULT_LANGUAGE
        block = (doc.meta or {}).get("mabolo")
        value = block.get("language") if isinstance(block, dict) else None
        return value.strip().lower() if is_language(value) else DEFAULT_LANGUAGE

    def is_git_repository(self) -> bool:
        """True only for a repository that writes inside this vault."""
        return git.is_repository_inside(self.root)

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
        """Every entry, or an error naming the first one that could not be read.

        A bare list means the list is complete. Returning a short one quietly was
        fine while only a test called this, but it is the natural way in for the
        index and the search, and an index built from a partial list looks whole
        and is not.
        """
        out = []
        for path in self.entry_paths():
            try:
                out.append(self.read_entry(path))
            except (MaboloError, OSError, UnicodeDecodeError) as exc:
                raise MaboloError(
                    f"{path} cannot be read ({exc}), so this is not every entry. "
                    "Run `mabolo validate` to see all of them at once."
                ) from None
        return out

    def readable_entries(self) -> tuple[list[Entry], list[Path]]:
        """Every entry that could be read, and the paths of those that could not."""
        out: list[Entry] = []
        unreadable: list[Path] = []
        for path in self.entry_paths():
            try:
                out.append(self.read_entry(path))
            except (MaboloError, OSError, UnicodeDecodeError):
                unreadable.append(path)
        return out, unreadable

    def validate(self) -> Report:
        return validate_vault(self.root, self.areas)

    # Writing

    def write_entry(self, entry: Entry, path: Path | None = None, *, allow_findings: bool = False) -> Path:
        """Write one entry. Committing it is a separate concern.

        Three things have to hold before anything is written, and each one was a
        way to lose data before it did:

        * **The target belongs to this entry.** Its name is a name and not a
          path, its folder is the one the entry's area names, and it ends in
          `.md`. Containment alone is not enough: `../persona/victim` stays
          inside the vault and would overwrite somebody else's entry.
        * **What was read is what gets judged.** The errors are counted on the
          mapping as it came off disk, not on the tidied view. The tolerant read
          drops an incomplete `generated` block, and validating only the result
          would call a file clean that is not.
        * **Nothing is written that the validator rejects**, unless the caller
          says it reports every finding itself, which is what `allow_findings`
          means.
        """
        target = self.resolve_write_target(entry, path)
        meta = entry.to_meta()
        if not allow_findings:
            expected = area_of(self.root, target)
            problems = [
                p
                for p in validate_meta(
                    meta, path=target, body=entry.body, expected_area=expected, areas=self.areas
                )
                if p.is_error
            ]
            # The raw mapping as well, whenever this entry came from a file: its
            # errors are the ones a rewrite would quietly tidy away.
            if entry.raw:
                seen = {p.code for p in problems}
                problems += [
                    p
                    for p in validate_meta(
                        entry.raw, path=target, body=entry.body, expected_area=expected, areas=self.areas
                    )
                    if p.is_error and p.code not in seen
                ]
            if problems:
                raise MaboloError(
                    f"{target.name} has {len(problems)} error(s) and was not written: "
                    + "; ".join(p.message for p in problems[:3])
                )
        frontmatter.write(target, meta, entry.body)
        return target

    def resolve_write_target(self, entry: Entry, path: Path | None = None) -> Path:
        """Where this entry may be written, or an error saying why it may not."""
        if path is None:
            return self.path_for(entry.area, entry.name)
        target = frontmatter.normalise_target(Path(path))
        if ".." in Path(path).parts:
            raise MaboloError(f"{path} steps through '..', and an entry is written to a plain path")
        if target.suffix != ".md":
            raise MaboloError(f"{target.name} is not a Markdown file, and every entry is one")
        self.check_name(target.stem)
        self.ensure_inside(target)
        if target.parent == self.root:
            raise MaboloError("an entry lives in an area folder, not in the vault root")
        area = area_of(self.root, target)
        if area is None or not (area in self.areas or is_project_area(area)):
            raise MaboloError(f"{target.parent} is not an area of this vault")
        if entry.area and entry.area != area:
            raise MaboloError(
                f"the entry says area {entry.area!r} and the path says {area!r}, "
                "and writing it would put it where nothing looks for it"
            )
        return target

    # Creating

    def plan(self) -> list[Action]:
        """What `init` would do, without doing any of it.

        Every index that `initialise` rewrites is listed, including the ones in
        folders nobody configured. The plan is what a person agrees to, and it
        used to name the area indexes only while the run rebuilt every index in
        the tree.
        """
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
        planned: set[Path] = set()
        for target, note in self._skeleton_targets():
            planned.add(target)
            if not target.exists():
                actions.append(Action("create", str(target), note))
            elif target.name == INDEX_FILE:
                # An index is derived, so `init` rebuilds it. Saying "exists"
                # would hide that the file is about to be replaced.
                actions.append(Action("rewrite", str(target), f"{note}, rebuilt from the entries"))
            else:
                actions.append(Action("exists", str(target), note))
        for directory in self._index_directories():
            target = directory / INDEX_FILE
            if target not in planned:
                actions.append(Action("rewrite", str(target), "rebuilt from the entries"))
        return actions

    def _skeleton(self) -> list[Skeleton]:
        """What belongs to a fresh vault, in one place.

        The plan prints it, `initialise` creates it, and the first commit names
        it. Written out three times, those three could and did drift apart: the
        plan said one thing and the run did another.
        """
        items = [
            Skeleton(self.root / ".gitignore", "what is derived and disposable", VAULT_GITIGNORE),
            Skeleton(self.index_file, f"bundle index, declares OKF {OKF_VERSION}"),
            Skeleton(self.log_file, "journal, newest first", LOG_HEADER),
            Skeleton(self.eval_dir / ".gitkeep", "eval cases, versioned with the entries", ""),
        ]
        for area in self.areas:
            items.append(Skeleton(self.area_dir(area) / INDEX_FILE, f"area {area}"))
        items.append(Skeleton(self.root / "project" / INDEX_FILE, "area project/<name>"))
        return items

    def _skeleton_targets(self) -> list[tuple[Path, str]]:
        """The same list as paths and notes, for the plan.

        The eval folder is shown rather than the `.gitkeep` inside it, because
        the folder is the thing a person recognises.
        """
        return [
            (self.eval_dir if item.path.name == ".gitkeep" else item.path, item.note)
            for item in self._skeleton()
        ]

    def _index_directories(self) -> list[Path]:
        """Every folder that gets an index.md, deepest first.

        Deepest first because a folder index has to exist before the index above
        it counts what is in it.
        """
        directories = {self.root}
        if self.root.is_dir():
            for path in self.entry_paths():
                directories.add(path.parent)
            for area in (*self.areas, "project"):
                directory = self.area_dir(area)
                if directory.is_dir():
                    directories.add(directory)
        return sorted(directories, key=lambda d: (-len(d.parts), str(d)))

    def looks_like_somebody_elses_folder(self) -> bool:
        """True for a folder that holds Markdown and is not a vault.

        `init` rewrites every index.md below its root. Pointed at a directory
        that belongs to something else, which one mistyped path is enough for,
        that replaces the landing page of whatever lives there.
        """
        if not self.root.is_dir() or self.is_initialised():
            return False
        try:
            return bool(markdown_files(self.root))
        except MaboloError:
            return True

    def initialise(self) -> list[Action]:
        """Create what is missing. Running it twice changes nothing."""
        done: list[Action] = []
        created_root = not self.root.is_dir()
        if self.looks_like_somebody_elses_folder():
            raise MaboloError(
                f"{self.root} already holds Markdown files and is not a vault. "
                "Mabolo rebuilds every index.md below its root, which would replace them. "
                "Choose an empty folder, or an existing vault."
            )
        self.root.mkdir(parents=True, exist_ok=True)
        done.append(Action("create" if created_root else "exists", str(self.root), "vault root"))

        for item in self._skeleton():
            if item.content is None:
                continue  # an index, written by rebuild_indexes below
            target = self.ensure_inside(item.path)
            shown = self.eval_dir if target.name == ".gitkeep" else target
            if target.exists():
                done.append(Action("exists", str(shown)))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.content, encoding="utf-8")
            done.append(Action("create", str(shown)))

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
        by_folder: dict[Path, list[Path]] = defaultdict(list)
        for path in self.entry_paths():
            by_folder[path.parent].append(path)
        directories = self._index_directories()
        # Read before anything is written: the root index is both the file that
        # declares the language and the file about to be replaced.
        language = self.language or self.declared_language()
        written = [self._write_index(d, by_folder, directories, language) for d in directories]
        return list(reversed(written))

    def _write_index(
        self,
        directory: Path,
        by_folder: dict[Path, list[Path]],
        directories: list[Path],
        language: str = DEFAULT_LANGUAGE,
    ) -> Path:
        """One index.md, built from the same file list the validator walks.

        `by_folder` is that list, grouped. Reaching for `glob` here instead meant
        the index and the validator disagreed about what an entry is: `glob` is
        case sensitive where the validator is not, and it descends into hidden
        folders where the validator does not. A vault would then hold an entry
        that validates and is missing from its own index, and the count in the
        root index would differ between two machines that cloned it.
        """
        is_root = directory == self.root
        target = self.ensure_inside(directory / INDEX_FILE)
        self._refuse_to_overwrite_hand_written(target)
        entries = sorted(by_folder.get(directory, []), key=lambda p: p.name)
        # An area with no entries in it is still listed: a fresh vault would
        # otherwise show nothing at all, and the areas are what a reader needs
        # first. Beyond the areas, a folder appears once it holds an entry.
        subdirs = sorted(
            {
                directory / folder.relative_to(directory).parts[0]
                for folder in {*by_folder, *directories}
                if folder != directory and folder.is_relative_to(directory)
            }
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
                count = sum(
                    len(found)
                    for folder, found in by_folder.items()
                    if folder.is_relative_to(sub)
                )
                lines.append(f"* [{escape_markdown(sub.name)}]({sub.name}/{INDEX_FILE}) - {count} entries")
            lines.append("")
        if not entries and not subdirs:
            lines += ["Nothing here yet.", ""]

        meta = (
            {"okf_version": OKF_VERSION, "mabolo": {"language": language}} if is_root else None
        )
        frontmatter.write(target, meta, "\n".join(lines))
        return target

    def _refuse_to_overwrite_hand_written(self, target: Path) -> None:
        """Only replace an index.md that Mabolo could have written itself.

        The question is not "does this look like an entry" but "could this be
        mine". Asking it the other way around meant that an unreadable file, or
        one with a frontmatter Mabolo does not write, was replaced and its
        contents gone: exactly the files a person most likely wrote by hand.
        A generated index carries no frontmatter, except at the root where it
        carries `okf_version` and the `mabolo` block that declares the language.
        """
        if not target.exists():
            return
        try:
            doc = frontmatter.read(target)
        except (MaboloError, OSError, UnicodeDecodeError) as exc:
            raise MaboloError(
                f"{target} cannot be read ({exc}), and Mabolo does not replace a file it cannot check. "
                "Move it aside, then rebuild the index."
            ) from None
        if doc.meta is None:
            return
        if set(doc.meta) <= {"okf_version", "mabolo"}:
            return
        raise MaboloError(
            f"{target} carries a frontmatter that Mabolo does not write, so it was not made here. "
            "Move it aside before Mabolo rebuilds the index in this folder."
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

    def git_initialise(self, branch: str = "main", paths: list[Path] | None = None) -> GitResult:
        """Create the repository and commit exactly the files that were created.

        The caller gets a result rather than a sentence, because "made no commit
        because git has no identity" printed at the end of a successful looking
        run is how a script concludes the vault is versioned when it is not.
        """
        if not git.available():
            return GitResult(False, "git is not installed, so the vault is a plain folder for now")
        existing = git.repository_dir(self.root)
        if existing is not None and not self.is_git_repository():
            return GitResult(
                False,
                f"{self.root}/.git points at {existing}, which is outside the vault. "
                "Mabolo does not commit into a repository somewhere else.",
            )
        if existing is None:
            self.git("init", "-b", branch)
        if git.identity_missing(self.root):
            return GitResult(
                False,
                "created the repository but made no commit: git has no user.email set. "
                "Set it, then commit the vault yourself.",
            )
        wanted = paths if paths is not None else self._skeleton_files()
        relative = []
        for path in wanted:
            try:
                relative.append(str(Path(path).relative_to(self.root)))
            except ValueError:
                continue
        if not relative:
            return GitResult(True, "nothing to commit")
        # In an index of its own: whatever the person had staged in this
        # repository stays staged and out of this commit.
        head = git.commit_paths(self.root, relative, "Create vault")
        if head is None:
            return GitResult(True, "nothing to commit, the vault is already in Git")
        return GitResult(True, f"committed the vault skeleton as {head}", revision=head)

    def _skeleton_files(self) -> list[Path]:
        """The files `init` itself creates, named one by one rather than by folder."""
        return [item.path for item in self._skeleton() if item.path.exists()]

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

