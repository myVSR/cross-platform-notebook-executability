#!/usr/bin/env python3
"""Start a kernel by name, as nbconvert will, and report which interpreter it really runs.

Usage (with the tool environment's python, JUPYTER_PATH pointing at the run's kernelspec):
    kernel_probe.py KERNEL_NAME [--timeout 120]
Prints JSON: status (ok | failed), executable, python_version, ipykernel_version,
sys_path_head (first entries, to confirm the resource's import path), error.
"""
from __future__ import annotations

import argparse
import json
import sys

CODE = ('import sys, json, platform\n'
        'try:\n'
        '    import ipykernel; v = ipykernel.__version__\n'
        'except Exception:\n'
        '    v = None\n'
        'print(json.dumps(dict(executable=sys.executable, python_version=platform.python_version(), '
        'ipykernel_version=v, sys_path_head=sys.path[:6])))\n')


def probe(kernel_name, timeout):
    from jupyter_client.manager import start_new_kernel
    manager = client = None
    try:
        manager, client = start_new_kernel(kernel_name=kernel_name, startup_timeout=timeout)
        output = []
        client.execute_interactive(CODE, timeout=timeout,
                                   output_hook=lambda m: output.append(m['content'].get('text', ''))
                                   if m['msg_type'] == 'stream' else None)
        return dict(status='ok', **json.loads(''.join(output).strip().splitlines()[-1]))
    except Exception as error:  # noqa: BLE001 - any failure to start or run is the result
        return dict(status='failed', error=f'{type(error).__name__}: {error}'[:500])
    finally:
        if client is not None:
            client.stop_channels()
        if manager is not None:
            manager.shutdown_kernel(now=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('kernel_name')
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args(argv)
    result = probe(args.kernel_name, args.timeout)
    print(json.dumps(result))
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
