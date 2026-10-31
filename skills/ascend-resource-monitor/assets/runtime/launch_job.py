#!/usr/bin/env python3
"""Declare a workload's origin for monitoring; this does not grant privileges."""
import argparse
import os


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True, choices=['agent', 'manual'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('Specify the workload command after --')
    env = os.environ.copy()
    env['ASCEND_MONITOR_JOB_ORIGIN'] = args.origin
    os.execvpe(command[0], command, env)


if __name__ == '__main__':
    main()
