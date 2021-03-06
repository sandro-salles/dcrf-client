import json
import linecache
import re
import subprocess
import types
from itertools import groupby
from pathlib import Path
from typing import Any, Dict, List

import pytest
from _pytest.fixtures import FuncFixtureInfo


SCRIPT_PATH = Path(__file__)
SCRIPT_DIR = SCRIPT_PATH.parent
MOCHA_RUNNER_PATH = SCRIPT_DIR / 'runner.ts'


class MochaCoordinator:
    def __init__(self):
        self._did_start = False

        self.proc = subprocess.Popen(
            args=['ts-node', MOCHA_RUNNER_PATH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        self.tests = None
        self._read_tests()

    @property
    def did_start(self):
        return self._did_start

    def start(self):
        """Begin execution of the test suite"""
        self._write()
        self._did_start = True

    def _read_tests(self) -> List[Dict[str, Any]]:
        if self.tests is None:
            event = self.expect('collect')
            self.tests = event['tests']
        return self.tests

    def write(self, type, **info):
        line = {
            'type': type,
            **info,
        }
        event = json.dumps(line)
        self._write(event)

    def _write(self, s: str = None):
        if s is not None:
            self.proc.stdin.write(s.encode('utf-8'))

        self.proc.stdin.write(b'\n')
        self.proc.stdin.flush()

    def read(self) -> Dict[str, Any]:
        line = self.proc.stdout.readline()
        event = json.loads(line)
        return event

    def expect(self, *types: str) -> Dict[str, Any]:
        event = self.read()
        if event['type'] not in types:
            str_types = ', '.join(types)
            raise ValueError(f'Expected one of {str_types}, but found: {event["type"]}')
        return event


coordinator = MochaCoordinator()


class MochaTest(pytest.Function):
    def __init__(self, *args, **kwargs):
        self._obj = self._testmethod

        super().__init__(*args, **kwargs)

    def _testmethod(self, live_server, **kwargs):
        coordinator.expect('test')
        coordinator.write('server info', url=live_server.url, ws_url=live_server.ws_url)

        event = coordinator.expect('pass', 'fail')
        if event['state'] == 'failed':
            message = event['err']
            stack = event['stack']

            match = re.search(
                r'at (?P<context>\S+) \((?P<file>.+):(?P<lineno>\d+):(?P<col>\d+)\)$',
                stack,
                re.MULTILINE,
            )
            if not match:
                raise RuntimeError(message)

            #
            # Juicy JS stack trace found! We can trick Python into printing the
            # relevant JS source, by creating a fake Python module with a raise
            # statement at the same line number, and filling Python's cache of
            # file sources (AKA linecache) with the actual JS code.
            #
            ###

            file = match.group('file')
            lineno = int(match.group('lineno'))

            ##
            # Fill line cache with the actual JS source
            #
            with open(file) as fp:
                source = fp.read()
                def getsource():
                    return source
                linecache.cache[file] = (getsource,)

            ###
            # Create a fake module, raising an exception from the same
            # line number as the error raised in the JS file.
            #
            mod = types.ModuleType(file)
            exc_msg = f'{message}\n\n{stack}'
            fake_source = '\n' * (lineno - 1) + f'raise RuntimeError({exc_msg!r})'
            co = compile(fake_source, file, "exec", dont_inherit=True)
            exec(co, mod.__dict__)


class MochaFile(pytest.Item, pytest.File):
    obj = None


def pytest_collection(session: pytest.Session):
    session.items = []

    for filename, tests in groupby(coordinator.tests, key=lambda test: test['file']):
        file = MochaFile(
            filename,
            parent=session,
            config=session.config,
            session=session,
        )


        for info in tests:
            requested_fixtures = ['live_server', '_live_server_helper']
            test = MochaTest(
                name='::'.join(info['parents']),
                parent=file,
                fixtureinfo=FuncFixtureInfo(
                    argnames=tuple(requested_fixtures),
                    initialnames=tuple(requested_fixtures),
                    names_closure=requested_fixtures,
                    name2fixturedefs={},
                ),
                keywords={
                    'django_db': pytest.mark.django_db(transaction=True),
                }
            )

            session.items.append(test)

    return session.items


def pytest_runtestloop(session):
    coordinator.start()
