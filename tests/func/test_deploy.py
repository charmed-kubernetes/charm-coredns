import subprocess
from pathlib import Path
from time import sleep

import pytest


CHARM_DIR = Path(__file__).parent.parent.parent.resolve()
SPEC_FILE = Path(__file__).parent / 'validate-dns-spec.yaml'


def test_charm():
    model = run('juju', 'switch').split('/')[-1]
    coredns_ready = run(
        'kubectl', 'get', 'pod', '-n', model, '-l', 'juju-app=coredns',
        '-o', 'jsonpath={..status.containerStatuses[0].ready}')
    assert coredns_ready == 'true'
    run('kubectl', 'apply', '-f', SPEC_FILE)
    try:
        wait_for_output('kubectl', 'get', 'pod/validate-dns',
                        expected='Running')
        for name in ("www.ubuntu.com", "kubernetes.default.svc.cluster.local"):
            run('kubectl', 'exec', 'validate-dns', '--', 'nslookup', name)
    finally:
        run('kubectl', 'delete', '-f', SPEC_FILE)


def run(*args):
    args = [str(a) for a in args]
    try:
        res = subprocess.run(args,
                             check=True,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        return res.stdout.decode('utf8').strip()
    except subprocess.CalledProcessError as e:
        pytest.fail(f'Command {args} failed ({e.returncode}):\n'
                    f'stdout:\n{e.stdout.decode("utf8")}\n'
                    f'stderr:\n{e.stderr.decode("utf8")}\n')


def wait_for_output(*args, expected='', timeout=3 * 60):
    args = [str(a) for a in args]
    output = None
    for attempt in range(int(timeout / 5)):
        output = run(*args)
        if expected in output:
            break
        sleep(5)
    else:
        pytest.fail(f'Timed out waiting for "{expected}" from {args}:\n{output}')
