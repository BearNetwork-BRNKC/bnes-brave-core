# Copyright (c) 2026 The Brave Authors. All rights reserved.
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at https://mozilla.org/MPL/2.0/.
"""Tests for `set_target_os`/`ensure_win_toolchain`'s `target_os` handling.

The behaviour under test is selected by a seeded `MODE` env var, so each case
drives one branch without needing a typed PROPERTIES message.
"""

from __future__ import annotations

import post_process

DEPS = ['chromium_checkout', 'env', 'file', 'path', 'step']


def RunSteps(api):
    mode = api.env.get('MODE')
    if mode == 'set_target_os':
        api.chromium_checkout.set_target_os(api.path.chromium_src,
                                            ['linux', 'mac', 'win'])
    elif mode == 'missing_gclient':
        api.chromium_checkout.set_target_os(api.path.chromium_src, ['win'])
    elif mode == 'ensure_checkout_target_os':
        api.chromium_checkout.ensure_checkout(
            target_os=['linux', 'win', 'android'])
    elif mode == 'win_toolchain_via_target_os':
        api.chromium_checkout.ensure_win_toolchain(['win'])
        _echo_toolchain_env(api)
    elif mode == 'win_toolchain_opt_out':
        api.env.set('DEPOT_TOOLS_WIN_TOOLCHAIN', '0')
        api.chromium_checkout.ensure_win_toolchain(['win'])
        _echo_toolchain_env(api)
    elif mode == 'no_windows_target_os':
        api.chromium_checkout.ensure_win_toolchain(['linux', 'mac'])
        _echo_toolchain_env(api)


def _echo_toolchain_env(api) -> None:
    # Surface the otherwise-internal hermetic toolchain env as a step so a test
    # can assert whether `ensure_win_toolchain` set it.
    toolchain = api.env.get('DEPOT_TOOLS_WIN_TOOLCHAIN_BASE_URL')
    if toolchain:
        api.step('win toolchain env', ['echo', toolchain])


def GenTests(api):
    # An existing .gclient with just `solutions` -> target_os is added, and the
    # existing solutions are carried forward into the regenerated spec.
    yield api.test(
        'preserves existing solutions',
        api.env.set('MODE', 'set_target_os'),
        api.path.files('b/.gclient'),
        api.post_process(post_process.MustRun, 'read .gclient'),
        api.post_process(post_process.MustRun, 'remove .gclient'),
        api.post_process(
            post_process.StepCommandRE, 'gclient config (target_os)',
            ['gclient', 'config', '--spec', r"(?s).*'src'.*target_os.*"]),
        api.post_process(post_process.StatusSuccess),
        api.post_process(post_process.DropExpectation),
    )
    # No .gclient at all -> refuse rather than silently doing nothing.
    yield api.test(
        'missing gclient raises',
        api.env.set('MODE', 'missing_gclient'),
        api.post_process(post_process.StatusException),
        api.post_process(post_process.DropExpectation),
        status='EXCEPTION',
    )
    # A non-literal assignment (e.g. a prior hand-edit calling a function) is
    # skipped rather than raising, while literal assignments alongside it
    # still carry forward.
    yield api.test(
        'skips non literal assignments',
        api.env.set('MODE', 'set_target_os'),
        api.path.files('b/.gclient'),
        api.step_data(
            'read .gclient',
            api.file.read_text("solutions = [{'name': 'src'}]\n"
                               "cache_dir = compute_cache_dir()\n"
                               "custom_vars = {'foo': 'bar'}\n")),
        api.post_process(post_process.StepCommandRE,
                         'gclient config (target_os)', [
                             'gclient', 'config', '--spec',
                             r"(?s).*'src'.*custom_vars.*target_os.*"
                         ]),
        api.post_process(post_process.StatusSuccess),
        api.post_process(post_process.DropExpectation),
    )
    # A .gclient with no `solutions` assignment is not a usable base.
    yield api.test(
        'gclient without solutions raises',
        api.env.set('MODE', 'set_target_os'),
        api.path.files('b/.gclient'),
        api.step_data('read .gclient',
                      api.file.read_text("cache_dir = '/b/cache'\n")),
        api.post_process(post_process.StatusException),
        api.post_process(post_process.DropExpectation),
        status='EXCEPTION',
    )
    # ensure_checkout(target_os=...) configures target_os before syncing.
    # `clone`'s own `gclient config` step would write the real .gclient on a
    # real machine; the simulated filesystem doesn't track that side effect,
    # so it's seeded directly for `set_target_os`'s follow-up read.
    yield api.test(
        'ensure_checkout configures target_os',
        api.env.set('MODE', 'ensure_checkout_target_os'),
        api.chromium_checkout.with_git_cache(),
        api.chromium_checkout.git_cache_populated(),
        api.path.files('b/.gclient'),
        api.post_process(post_process.MustRun, 'clone from git cache'),
        api.post_process(post_process.MustRun, 'gclient config (target_os)'),
        api.post_process(
            post_process.StepCommandRE, 'gclient config (target_os)',
            ['gclient', 'config', '--spec', r'(?s).*target_os.*']),
        api.post_process(post_process.StatusSuccess),
        api.post_process(post_process.DropExpectation),
    )
    # 'win' in target_os sets the hermetic toolchain URL even on a non-Windows
    # host, since cross-platform deps for 'win' still need to be fetched.
    yield api.test(
        'win in target_os sets toolchain on any host',
        api.env.set('MODE', 'win_toolchain_via_target_os'),
        api.post_process(post_process.MustRun, 'win toolchain env'),
        api.post_process(
            post_process.StepCommandContains, 'win toolchain env', [
                'https://vhemnu34de4lf5cj6bx2wwshyy0egdxk.lambda-url.us-west-'
                '2.on.aws/windows-hermetic-toolchain/'
            ]),
        api.post_process(post_process.StatusSuccess),
        api.post_process(post_process.DropExpectation),
    )
    # An explicit opt-out (DEPOT_TOOLS_WIN_TOOLCHAIN already set) is respected.
    yield api.test(
        'explicit opt out is respected',
        api.env.set('MODE', 'win_toolchain_opt_out'),
        api.post_process(post_process.DoesNotRun, 'win toolchain env'),
        api.post_process(post_process.StatusSuccess),
        api.post_process(post_process.DropExpectation),
    )
    # No 'win' among target_os, non-Windows host -> nothing is set.
    yield api.test(
        'no windows deps in play',
        api.env.set('MODE', 'no_windows_target_os'),
        api.post_process(post_process.DoesNotRun, 'win toolchain env'),
        api.post_process(post_process.StatusSuccess),
        api.post_process(post_process.DropExpectation),
    )
