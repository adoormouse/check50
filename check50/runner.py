import collections
from contextlib import contextmanager
import concurrent.futures as futures
import enum
import functools
import inspect
import importlib
import gettext
import os
from pathlib import Path
import shutil
import signal
import tempfile
import traceback

import attr

from . import internal
from .api import log, Failure, _copy, _log, _data

_check_names = []


class Status(enum.Enum):
    Pass = True
    Fail = False
    Skip = None


@attr.s(slots=True)
class CheckResult:
    name = attr.ib()
    description = attr.ib()
    status = attr.ib(default=None, converter=Status)
    log = attr.ib(default=[])
    cause = attr.ib(default=None)
    data = attr.ib(default={})
    dependency = attr.ib(default=None)

    @classmethod
    def from_check(cls, check, *args, **kwargs):
        return cls(name=check.__name__, description=_(check.__doc__),
                   dependency=check._check_dependency.__name__ if check._check_dependency else None,
                   *args,
                   **kwargs)


class Timeout(Failure):
    def __init__(self, seconds):
        super().__init__(rationale=_("check timed out after {} seconds").format(seconds))


@contextmanager
def _timeout(seconds):
    def _handle_timeout(*args):
        raise Timeout(seconds)

    signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)


def check(dependency=None, timeout=60):
    """Mark function as a check.

    :param dependency: the check that this check depends on
    :type dependency: function
    :param timeout: maximum number of seconds the check can run
    :type timeout: int

    When a check depends on another, the former will only run if the latter passes.
    Additionally, the dependent check will inherit the filesystem of its dependency.
    This is particularly useful when writing e.g., a ``compiles`` check that compiles a
    student's program (and checks that it compiled successfully). Any checks that run the
    student's program will logically depend on this check, and since they inherit the
    resulting filesystem of the check, they will immidiately have access to the compiled
    program without needing to recompile.

    Example usage::

        @check50.check() # Mark 'exists' as a check
        def exists():
            \"""hello.c exists\"""
            check50.exists("hello.c")

        @check50.check(exists) # Mark 'compiles' as a check that depends on 'exists'
        def compiles():
            \"""hello.c compiles\"""
            check50.c.compile("hello.c")

        @check50.check(compiles)
        def prints_hello():
            \"""prints "Hello, world!\\\\n\"""
            # Since 'prints_hello', depends on 'compiles' it inherits the compiled binary
            check50.run("./hello").stdout("[Hh]ello, world!?\\n", "hello, world\\n").exit()

    """
    def decorator(check):

        # Modules are evaluated from the top of the file down, so _check_names will
        # contain the names of the checks in the order in which they are declared
        _check_names.append(check.__name__)
        check._check_dependency = dependency

        @functools.wraps(check)
        def wrapper(checks_root, dependency_state):
            # Result template
            result = CheckResult.from_check(check)
            # Any shared (returned) state
            state = None

            try:
                # Setup check environment
                internal.run_dir = checks_root / check.__name__
                src_dir = checks_root / (dependency.__name__ if dependency else "-")
                shutil.copytree(src_dir, internal.run_dir)
                os.chdir(internal.run_dir)

                # Run registered functions before/after running check
                with internal.register, _timeout(seconds=timeout):
                    args = (dependency_state,) if inspect.getargspec(check).args else ()
                    state = check(*args)
            except Failure as e:
                result.status = Status.Fail
                result.cause = e.payload
            except BaseException as e:
                result.status = Status.Skip
                result.cause = {"rationale": _("check50 ran into an error while running checks!")}
                log(repr(e))
                for line in traceback.format_tb(e.__traceback__):
                    log(line.rstrip())
                log(_("Contact sysadmins@cs50.harvard.edu with the URL of this check!"))
            else:
                result.status = Status.Pass
            finally:
                result.log = _log
                result.data = _data
                return result, state
        return wrapper
    return decorator


# Probably shouldn't be a class
class CheckRunner:
    def __init__(self, checks_path):

        # TODO: Naming the module "checks" is arbitray. Better name?
        self.checks_spec = importlib.util.spec_from_file_location("checks", checks_path)

        # Clear check_names, import module, then save check_names. Not thread safe.
        # Ideally, there'd be a better way to extract declaration order than @check mutating global state,
        # but there are a lot of subtleties with using `inspect` or similar here
        _check_names.clear()
        check_module = importlib.util.module_from_spec(self.checks_spec)
        self.checks_spec.loader.exec_module(check_module)
        self.check_names = _check_names.copy()
        _check_names.clear()

        # Map each check to tuples containing the names and descriptions of the checks that depend on it
        self.child_map = collections.defaultdict(set)
        for name, check in inspect.getmembers(check_module, lambda f: hasattr(f, "_check_dependency")):
            dependency = check._check_dependency.__name__ if check._check_dependency is not None else None
            self.child_map[dependency].add((name, check.__doc__))

        # TODO: Check for deadlocks (Khan's algorithm?)

    def run(self, files, working_area):
        """
        Run checks concurrently.
        Returns a list of CheckResults ordered by declaration order of the checks in the imported module
        """

        # Ensure that dictionary is ordered by check declaration order (via self.check_names)
        # NOTE: Requires CPython 3.6. If we need to support older versions of Python, replace with OrderedDict.
        results = {name: None for name in self.check_names}
        executor = futures.ProcessPoolExecutor()

        checks_root = working_area.parent

        # Start all checks that have no dependencies
        not_done = set(executor.submit(run_check(name, self.checks_spec, checks_root))
                       for name, _ in self.child_map[None])
        not_passed = []

        while not_done:
            done, not_done = futures.wait(not_done, return_when=futures.FIRST_COMPLETED)
            for future in done:
                result, state = future.result()
                results[result.name] = result
                if result.status is Status.Pass:
                    for child_name, _ in self.child_map[result.name]:
                        not_done.add(executor.submit(
                            run_check(child_name, self.checks_spec, checks_root, state)))
                else:
                    not_passed.append(result.name)

        for name in not_passed:
            self._skip_children(name, results)

        return results.values()

    def _skip_children(self, check_name, results):
        """Recursively skip the children of check_name (presumably because check_name did not pass)."""
        for name, description in self.child_map[check_name]:
            if results[name] is None:
                results[name] = CheckResult(name=name, description=_(description),
                                            status=Status.Skip,
                                            dependency=check_name,
                                            cause={"rationale": _("can't check until a frown turns upside down")})
                self._skip_children(name, results)


class run_check:
    """
    Hack to get around the fact that `pickle` can't serialize closures.
    This class is essentially a function that reimports the check module and runs the check.
    """

    def __init__(self, check_name, spec, checks_root, state=None):
        self.check_name = check_name
        self.spec = spec
        self.checks_root = checks_root
        self.state = state

    def __call__(self):
        mod = importlib.util.module_from_spec(self.spec)
        self.spec.loader.exec_module(mod)
        return getattr(mod, self.check_name)(self.checks_root, self.state)
