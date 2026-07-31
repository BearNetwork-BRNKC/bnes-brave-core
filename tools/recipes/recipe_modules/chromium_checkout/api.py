# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""The `chromium_checkout` module API."""

from __future__ import annotations

import ast
from collections.abc import Sequence
import contextlib
import functools
import logging
from pathlib import Path
import re
import subprocess

from recipe_api import RecipeApi

# A file that is reliably present in any Chromium checkout, used as a token to
# decide whether a path holds a valid repo.
CHROME_VERSION_FILE = Path('chrome/VERSION')

# Hermetic Windows toolchain base URL, so the checkout can build without a local
# Visual Studio install. Set only when not already configured by the caller.
WIN_HERMETIC_TOOLCHAIN_BASE_URL = (
    'https://vhemnu34de4lf5cj6bx2wwshyy0egdxk.lambda-url.us-west-'
    '2.on.aws/windows-hermetic-toolchain/')

# The URL for Chromium's googlesource.
CHROMIUM_URL = 'https://chromium.googlesource.com/chromium/src.git'

# Resolves the `GYP_MSVS_HASH_<hash>` override for the hermetic Windows
# toolchain. See `_pin_win_toolchain_hash`.
_WIN_TOOLCHAIN_HASH_SCRIPT = (Path(__file__).resolve().parent / 'resources' /
                              'win_toolchain_hash.py')

# `.gclient` content `set_target_os`'s `read .gclient` step returns under
# simulation when a test doesn't seed something more specific -- shaped like
# what `checkout_ref`'s `gclient config --unmanaged` step actually writes.
DEFAULT_GCLIENT_SPEC = ("solutions = [\n"
                        "  {\n"
                        "    'name': 'src',\n"
                        "    'url': "
                        f"'{CHROMIUM_URL}',\n"
                        "    'deps_file': 'DEPS',\n"
                        "    'managed': False,\n"
                        "  },\n"
                        "]\n")


def _parse_gclient_spec(content: str) -> dict[str, object]:
    """Return the top-level literal assignments in a `.gclient` spec.

    A `.gclient` is a Python file of plain assignments (`solutions = [...]`,
    optionally `target_os`, `cache_dir`, ...). Each right-hand side is a
    literal, so they are read with `ast.literal_eval` rather than executing
    the file. Assignments whose value is not a literal are skipped.
    """
    tree = ast.parse(content)
    config: dict[str, object] = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            try:
                config[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return config


def _is_tag_ref(ref: str) -> bool:
    """Whether *ref* looks like a Chromium release tag (e.g. `150.0.7850.1`),
    as opposed to a branch name or a commit hash."""
    return bool(re.fullmatch(r'\d+\.\d+\.\d+\.\d+', ref))


def _is_commit_hash_ref(ref: str) -> bool:
    """Whether *ref* looks like a full git commit hash, as opposed to a
    branch name."""
    return bool(re.fullmatch(r'[0-9a-fA-F]{40}', ref))


def _is_fully_qualified_ref(ref: str) -> bool:
    """Whether *ref* is already a fully-qualified ref path.
    """
    return ref.startswith('refs/')


class ChromiumCheckoutApi(RecipeApi):
    """Clones, syncs, and validates a Chromium `src/` checkout."""

    @contextlib.contextmanager
    def chromium_layout(self):
        """Context manager entered before any Chromium checkout operation.

        Responsible for basic environment initialization.
        """
        with self.m.context(
                env=
            {
                # CHROME_HEADLESS makes sure that running `gclient
                # runhooks` and other tools don't require user
                # interaction.
                'CHROME_HEADLESS': '1',
            }):
            yield

    def _with_chromium_layout(fn):
        """Decorator applying `chromium_layout()` to a bound
        `ChromiumCheckoutApi` method.

        INTERNAL: decorates `ChromiumCheckoutApi` member functions only; do
        not use outside this class/module.
        """

        @functools.wraps(fn)
        def inner(self, *args, **kwargs):
            with self.chromium_layout():
                return fn(self, *args, **kwargs)

        return inner

    @_with_chromium_layout
    def ensure_checkout(self,
                        *,
                        chromium_src: str | Path | None = None,
                        ref: str | None = None,
                        git_cache: str | Path | None = None,
                        depth: int | None = None,
                        target_os: Sequence[str] | None = None) -> Path:
        """Guarantee a Chromium checkout at *chromium_src*, optionally on *ref*.

        Clones a fresh checkout if *chromium_src* is not already a valid
        Chromium repo, then checks out *ref* if given.

        Args:
            chromium_src: Path to the Chromium `src/` directory. Defaults to the
                `path` module's `chromium_src`, the standard job layout.
            ref: Optional git ref (branch, tag, or commit) to check out.
            git_cache: Optional explicit git cache directory. When given it sets
                `GIT_CACHE_PATH`; otherwise an existing `GIT_CACHE_PATH` in the
                environment is used as-is.
            depth: Optional history depth for this working checkout (see
                `checkout_ref`). The shared git-cache mirror is always
                populated with full history regardless; `None` here checks
                out full history too.
            target_os: Optional gclient `target_os` list to configure before the
                sync, so dependencies for those platforms are fetched too (e.g.
                `('win', 'mac', 'linux', 'android')`). Applied whether
                *chromium_src* was just cloned or already existed, since a
                reused checkout may predate this target_os.

        Returns:
            The resolved absolute `src/` path.
        """
        if chromium_src is None:
            chromium_src = self.m.path.chromium_src
        chromium_src = self.m.path.abs(chromium_src)

        if git_cache is not None:
            self.set_git_cache(git_cache or None)
        self.validate_git_cache()

        # depot_tools provides `fetch`/`gclient`/`git cache`, needed whether we
        # clone or operate on an existing checkout.
        self.m.depot_tools.ensure_on_path()

        self.checkout_ref(chromium_src, ref, depth=depth, target_os=target_os)
        return chromium_src

    def set_target_os(self, chromium_src: str | Path,
                      target_os: Sequence[str]) -> None:
        """Configure gclient `target_os` so cross-platform deps are synced.

        Args:
            chromium_src: Path to the Chromium `src/` directory.
            target_os: gclient OS names to sync, e.g. `('win', 'mac', 'linux')`.

        Raises:
            RuntimeError: If `.gclient` is missing or declares no solutions.
        """
        chromium_src = self.m.path.abs(chromium_src)
        parent = chromium_src.parent
        gclient_file = parent / '.gclient'
        if not self.m.path.is_file(gclient_file):
            raise RuntimeError(
                f'.gclient not found at {gclient_file}; the checkout must be '
                'cloned before target_os can be set')

        content = self.m.file.read_text('read .gclient',
                                        gclient_file,
                                        test_data=DEFAULT_GCLIENT_SPEC)
        config = _parse_gclient_spec(content)
        if 'solutions' not in config:
            raise RuntimeError(f'no solutions found in {gclient_file}')

        # Preserve every existing assignment (solutions, custom vars, cache_dir,
        # ...) and just (re)set target_os; emit as a spec gclient can exec.
        config['target_os'] = list(target_os)
        spec = '\n'.join(f'{key} = {value!r}' for key, value in config.items())

        # `gclient config` refuses to overwrite an existing .gclient, so remove
        # it first; the spec we just built carries its contents forward. Named
        # distinctly from `checkout_ref`'s own `gclient config` step, since both
        # can run in the same `ensure_checkout` call.
        self.m.file.remove('remove .gclient', gclient_file)
        self.m.step('gclient config (target_os)',
                    ['gclient', 'config', '--spec', spec],
                    cwd=parent)
        logging.info('Regenerated %s with target_os=%s', gclient_file,
                     list(target_os))

    def ensure_win_toolchain(self,
                             target_os: Sequence[str] | None = None) -> bool:
        """Point depot_tools at the hermetic Windows toolchain when needed.

        Windows dependencies are synced whenever the host is Windows or `win`
        is among *target_os*; in either case gclient needs the hermetic
        toolchain URL so it can build without a local Visual Studio install.
        No-op when no Windows deps are in play, when the caller opted out via
        `DEPOT_TOOLS_WIN_TOOLCHAIN`, or when the URL is already set.

        Args:
            target_os: gclient target OS list for the sync, if any.

        Returns:
            Whether the hermetic toolchain is (now) in effect -- i.e. Windows
            deps are in play and the caller hasn't opted out via
            `DEPOT_TOOLS_WIN_TOOLCHAIN`. Callers use this to decide whether the
            toolchain's hash needs pinning (see `_pin_win_toolchain_hash`).
        """
        targeting_windows = (self.m.platform.is_win
                             or (target_os is not None and 'win' in target_os))
        using_hermetic = (targeting_windows
                          and 'DEPOT_TOOLS_WIN_TOOLCHAIN' not in self.m.env)
        if using_hermetic:
            self.m.env.set('DEPOT_TOOLS_WIN_TOOLCHAIN_BASE_URL',
                           WIN_HERMETIC_TOOLCHAIN_BASE_URL)
        return using_hermetic

    def validate_git_cache(self) -> str:
        """Require `GIT_CACHE_PATH` to be set and point to a real directory.

        git/gclient honour `GIT_CACHE_PATH` to share object storage across
        checkouts. The pipeline mandates a cache, so a missing value is a hard
        error -- we refuse to run an uncached checkout -- as is a value that
        does not point at an existing directory.

        Returns:
            The current `GIT_CACHE_PATH` value.

        Raises:
            RuntimeError: If `GIT_CACHE_PATH` is unset or not a directory. Set
                it in the environment or via `set_git_cache()` beforehand.
        """
        git_cache_path = self.m.env.get('GIT_CACHE_PATH')
        if not git_cache_path:
            raise RuntimeError(
                'GIT_CACHE_PATH is not set; a shared git cache is required. '
                'Set it in the environment or via set_git_cache() before '
                'running the checkout.')
        if not self.m.path.is_dir(git_cache_path):
            raise RuntimeError(
                f'GIT_CACHE_PATH is not a valid directory: {git_cache_path}')
        logging.info('Using GIT_CACHE_PATH=%s', git_cache_path)
        return git_cache_path

    def set_git_cache(self, path: str | Path | None = None) -> Path:
        """Set `GIT_CACHE_PATH` for subsequent git/gclient steps.

        Mirrors `build_rust_toolchain.py`'s `--with-git-cache` handling: an
        explicit *path* is used as-is (user-expanded); otherwise it defaults to
        `<home>/cache` (`USERPROFILE` on Windows, `HOME` elsewhere), the layout
        our CI bakes the cache under. The directory must already exist, and
        `GIT_CACHE_PATH` must not already be set -- refusing to clobber an
        existing value avoids masking a misconfiguration.

        Args:
            path: Explicit cache directory, or None/empty to use `<home>/cache`.

        Returns:
            The `Path` that `GIT_CACHE_PATH` was set to.

        Raises:
            RuntimeError: If `GIT_CACHE_PATH` is already set, or the resolved
                directory does not exist.
        """
        if 'GIT_CACHE_PATH' in self.m.env:
            raise RuntimeError('GIT_CACHE_PATH is already set in the '
                               'environment.')

        if path:
            git_cache_path = self.m.path.abs(path)
        else:
            home_var = 'USERPROFILE' if self.m.platform.is_win else 'HOME'
            home = self.m.env.get(home_var) or self.m.path.home()
            git_cache_path = Path(home) / 'cache'

        if not self.m.path.is_dir(git_cache_path):
            raise RuntimeError(
                f'GIT_CACHE_PATH is not a valid directory: {git_cache_path}')

        self.m.env.set('GIT_CACHE_PATH', str(git_cache_path))
        logging.info('Set GIT_CACHE_PATH=%s', git_cache_path)
        return git_cache_path

    def has_valid_checkout(self, chromium_src: str | Path) -> bool:
        """Return whether *chromium_src* points to a valid Chromium repo."""
        chromium_src = Path(chromium_src)
        # `chrome/VERSION` is an unmistakable trait of a proper checkout.
        if not self.m.path.exists(chromium_src / CHROME_VERSION_FILE):
            return False

        logging.info('Checking for valid Chromium repo at %s', chromium_src)
        try:
            self.m.step(
                'check chrome/VERSION',
                ['git', 'log', '-1', '--oneline',
                 str(CHROME_VERSION_FILE)],
                cwd=chromium_src)
        except (subprocess.CalledProcessError, OSError):
            return False
        return True

    def checkout_ref(self,
                     chromium_src: str | Path,
                     ref: str | None = None,
                     *,
                     should_clone: bool = True,
                     depth: int | None = None,
                     target_os: Sequence[str] | None = None) -> None:
        """Ensure *chromium_src* is checked out at *ref*.

        Args:
            chromium_src: Path to the Chromium `src/` directory.
            ref: Git ref (branch, tag, or commit) to check out. `origin/HEAD`
                if not given and *chromium_src* needs cloning; a no-op if not
                given and *chromium_src* is already checked out (unless
                *target_os* is also given).
            should_clone: Whether cloning *chromium_src* is allowed if it
                doesn't already hold a valid checkout (the default). Set to
                False to require an existing checkout, raising instead of
                cloning one.
            depth: Optional history depth for this working checkout.
            target_os: Optional gclient `target_os` list to configure before
                the sync, so dependencies for those platforms are fetched too.
                Configured (and synced) even when *ref* is not given, so a
                reused checkout that predates this target_os still picks it up.

        If *chromium_src* isn't a valid checkout yet, rather than a plain
        network clone, `git cache populate` fetches into a persistent,
        shared bare mirror under `GIT_CACHE_PATH`. The mirror is populated with
        *ref* up front, and the working checkout is pointed straight at it.

        Otherwise, *chromium_src* already exists and its current state
        (branch/tag/commit) isn't known ahead of time, so it's re-pointed at
        the mirror and *ref* is fetched and checked out explicitly.
        """
        chromium_src = Path(chromium_src)
        is_tag = bool(ref and _is_tag_ref(ref))
        is_commit = bool(ref and not is_tag and _is_commit_hash_ref(ref))
        is_qualified_ref = bool(ref and not is_tag and not is_commit
                                and _is_fully_qualified_ref(ref))
        populate_ref = f'refs/tags/{ref}' if is_tag else (
            None if is_commit else ref)
        git_cache_path = self.validate_git_cache()

        if not self.has_valid_checkout(chromium_src):
            if not should_clone:
                raise RuntimeError(
                    f'No valid Chromium checkout at {chromium_src}, and '
                    'should_clone is False.')
            logging.info('Chromium src not found at %s, cloning...',
                         chromium_src)

            self.m.path.mkdir(chromium_src.parent)
            # Writes the `.gclient` solution file so `gclient sync` (once
            # checked out below) knows about the `src` solution.
            self.m.step('gclient config', [
                'gclient', 'config', '--name', 'src', '--unmanaged',
                CHROMIUM_URL
            ],
                        cwd=chromium_src.parent)

            mirror_dir = self._populate_git_cache(
                git_cache_path,
                CHROMIUM_URL,
                ref=populate_ref,
                commit=ref if is_commit else None,
                populate_step='git cache populate',
                exists_step='git cache exists')
            # `--local --shared`: a same-volume, hardlink-sharing clone of
            # the mirror, effectively free compared to a network clone.
            depth_args = ['--depth', str(depth)] if depth else []
            self.m.step('clone from git cache', [
                'git', 'clone', '--no-checkout', '--local', '--shared',
                *depth_args, mirror_dir, chromium_src
            ])
            self._disable_git_gc(chromium_src)

            if is_qualified_ref:
                # `ref` is a fully-qualified ref outside `refs/heads/*` and
                # `refs/tags/*` (e.g. a Chromium release branch under
                # `refs/branch-heads/*`).
                self.m.step('fetch ref',
                            ['git', 'fetch', *depth_args, 'origin', ref],
                            cwd=chromium_src)
                self.m.step('checkout ref',
                            ['git', 'checkout', '--force', 'FETCH_HEAD'],
                            cwd=chromium_src)
            else:
                checkout_target = populate_ref or ref or 'origin/HEAD'
                step_name = ('checkout tag'
                             if is_tag else 'checkout commit' if is_commit else
                             'checkout ref' if ref else 'checkout origin/HEAD')
                self.m.step(
                    step_name,
                    ['git', 'checkout', '--force', checkout_target, '--'],
                    cwd=chromium_src)
            if not ref and not target_os:
                return
            if ref:
                # `origin`'s push url should still point at the real remote,
                # not the local mirror `git clone` just set it to.
                self.m.step(
                    'restore origin push url',
                    ['git', 'remote', 'set-url', '--push', 'origin',
                     CHROMIUM_URL],
                    cwd=chromium_src)
        elif ref:
            # Already a valid checkout: its current state (branch/tag/commit)
            # is unknown ahead of time, so re-pointing it at `ref` needs an
            # explicit fetch+checkout.
            logging.info('Checking out Chromium ref %s', ref)
            mirror_dir = self._populate_git_cache(
                git_cache_path,
                CHROMIUM_URL,
                ref=populate_ref,
                commit=ref if is_commit else None,
                populate_step='git cache populate for ref',
                exists_step='git cache exists for ref')

            # `chromium_src` may already exist from before this checkout
            # started using a git cache mirror at all, so point `origin` at
            # the mirror unconditionally. Everything below then runs as
            # local disk I/O instead of talking to the real remote.
            self.m.step('point origin at git cache',
                        ['git', 'remote', 'set-url', 'origin', mirror_dir],
                        cwd=chromium_src)
            self.m.step(
                'restore origin push url',
                ['git', 'remote', 'set-url', '--push', 'origin', CHROMIUM_URL],
                cwd=chromium_src)

            # `chromium_src` may already be shallow at a different commit
            # than this ref (e.g. re-checking out a branch after it moved
            # on): a plain `git fetch` tries to extend the existing shallow
            # history and fails outright ("did not send all necessary
            # objects") once the (fully-populated) mirror can no longer
            # connect the two. Passing `--depth` here instead negotiates a
            # fresh, self-contained shallow window for the requested ref,
            # independent of that connection.
            depth_args = ['--depth', str(depth)] if depth else []
            if is_tag:
                # Chromium release tag (e.g. `150.0.7850.1`): fetch it as a
                # tag so it lands at `refs/tags/<ref>` in the local repo.
                self.m.step('fetch tag', [
                    'git', 'fetch', *depth_args, '--no-tags', 'origin',
                    f'refs/tags/{ref}:refs/tags/{ref}'
                ],
                            cwd=chromium_src)
            else:
                # A branch name or a bare commit hash both resolve directly
                # against `origin` -- no destination refspec needed.
                self.m.step('fetch commit' if is_commit else 'fetch ref',
                            ['git', 'fetch', *depth_args, 'origin', ref],
                            cwd=chromium_src)

            # A manual `git checkout --force` rather than `gclient sync -r
            # <ref>` sidesteps a gclient bug; see
            # https://github.com/brave/brave-browser/issues/44921.
            self.m.step('checkout FETCH_HEAD',
                        ['git', 'checkout', '--force', 'FETCH_HEAD'],
                        cwd=chromium_src)
        elif not target_os:
            # Already a valid checkout, no `ref`, and nothing else requested:
            # nothing to do.
            return

        # `chromium_src` is now checked out at `ref` (or left as-is if already
        # valid and no ref was requested) -- build hermetically without a
        # local VS install, unless the caller has already made an explicit
        # choice about the toolchain.
        using_hermetic_win_toolchain = self.ensure_win_toolchain(target_os)
        if using_hermetic_win_toolchain:
            # This is used by `gclient runhooks`.
            self._pin_win_toolchain_hash(chromium_src)

        # Configure target platforms before the sync below, so it pulls their
        # deps in the same pass.
        if target_os:
            self.set_target_os(chromium_src, target_os)

        self.m.step('gclient sync', ['gclient', 'sync', '--force', '-D'],
                    cwd=chromium_src)

    def _pin_win_toolchain_hash(self, chromium_src: Path) -> None:
        """Point `GYP_MSVS_HASH_<hash>` at Brave's republished toolchain.

        `build/vs_toolchain.py` pins a `TOOLCHAIN_HASH`, which the
        `win_toolchain` gclient hook resolves to `<TOOLCHAIN_HASH>.zip` on
        Google's own toolchain bucket. Overriding
        `DEPOT_TOOLS_WIN_TOOLCHAIN_BASE_URL` points the hook at our own bucket.
        `GYP_MSVS_HASH_<TOOLCHAIN_HASH>` is the override
        `_GetDesiredVsToolchainHashes` (in `build/vs_toolchain.py`) reads to
        substitute a different hash, so setting it to the hash Brave actually
        published the archive under.

        Nothing is set if an index with cannot be found with a redirect.
        """
        vpython3 = self.m.depot_tools.vpython3()
        result = self.m.step('resolve win toolchain hash', [
            vpython3, '-u', _WIN_TOOLCHAIN_HASH_SCRIPT,
            chromium_src / 'build' / 'vs_toolchain.py',
            WIN_HERMETIC_TOOLCHAIN_BASE_URL, '--json-output',
            self.m.json.output()
        ],
                             step_test_data=self.test_api.win_toolchain_hash)
        info = result.json.output
        if info['published_hash']:
            self.m.env.set(f"GYP_MSVS_HASH_{info['toolchain_hash']}",
                           info['published_hash'])

    def fetch_tags(self, chromium_src: str | Path) -> None:
        """Fetch every tag from origin into the *chromium_src* checkout.

        The checkout is backed by the shared git cache, so fetching tags here
        also lands them in the cache -- which is what a downstream mirroring
        step later reads and publishes. `gclient sync` fetches with
        `--no-tags`, so tags would otherwise never make it into the cache.

        Args:
            chromium_src: Path to the Chromium `src/` directory.
        """
        chromium_src = self.m.path.abs(chromium_src)
        self.m.step('fetch tags', ['git', 'fetch', '--tags', 'origin'],
                    cwd=chromium_src)

    def _populate_git_cache(self,
                            git_cache_path: str | Path,
                            url: str,
                            *,
                            ref: str | None = None,
                            commit: str | None = None,
                            populate_step: str,
                            exists_step: str) -> str:
        """Populate (or refresh) the shared bare mirror for *url*.

        `git cache populate` fetches into a persistent bare mirror under
        `GIT_CACHE_PATH`, which is reused across every checkout and build on
        this machine, rather than the working checkout talking to the remote
        directly.

        Args:
            git_cache_path: `GIT_CACHE_PATH` (the `--cache-dir` for `git
                cache`).
            url: The repo to mirror.
            ref: An additional ref (a plain branch name, or a fully-qualified
                ref such as `refs/tags/<tag>` or `refs/branch-heads/<n>`) to
                fetch into the mirror, beyond its default `refs/heads/*`.
            commit: An additional bare commit hash to fetch into the mirror.
            populate_step: Step name for the `git cache populate` call.
            exists_step: Step name for the `git cache exists` call.

        Returns:
            The absolute path to the mirror directory.
        """
        populate_cmd = [
            'git', 'cache', 'populate', '--cache-dir', git_cache_path, url,
            '--reset-fetch-config', '--no-fetch-tags'
        ]
        if ref:
            populate_cmd.extend(['--ref', ref])
        if commit:
            populate_cmd.extend(['--commit', commit])
        self.m.step(populate_step, populate_cmd)

        return self.m.step(exists_step, [
            'git', 'cache', 'exists', '--quiet', '--cache-dir', git_cache_path,
            url
        ],
                           stdout=self.m.raw_io.output_text()).stdout.strip()

    def _disable_git_gc(self, chromium_src: str | Path) -> None:
        """Disable background gc in *chromium_src*.

        A shared, long-lived checkout can have several tools touching it
        around the same time, and an auto-triggered `git gc` racing with them
        (or being killed midway) can corrupt the repo.
        """
        for key in ('gc.auto', 'gc.autodetach', 'gc.autopacklimit'):
            self.m.step(f'git config {key}=0', ['git', 'config', key, '0'],
                        cwd=chromium_src)
