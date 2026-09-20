# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Lei Zhou, Linaro
#
# Domain adapters. A domain is where a role executes: the machine this runs
# on, a guest, a driver domain, an R-class core. Everything above this
# package is written against the five verbs in `local.py`'s docstring and
# nothing else, which is what makes a second domain an adapter rather than a
# change to the session logic.
#
# Only the local adapter ships. A remote one waits for a campaign that needs
# it — but the interface is fixed now, because an interface discovered after
# the fact is an interface shaped by one implementation.
from .local import LocalAdapter, AdapterError          # noqa: F401
