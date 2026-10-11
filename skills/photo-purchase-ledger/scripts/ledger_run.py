"""Portable staged purchase-ledger execution with bounded outputs."""
import argparse
import sys
from pathlib import Path

from ledger_io import add_summary_arguments, emit_summary, write_new_json
from ledger_model import inspect_ledger
from ledger_jobs import prepare
from ledger_local import apply_local, verify_local, accept_visual, finish, publish_local
from ledger_state import locked_run
from ledger_online import online_plan, verify_online


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('apply-local', 'verify-local', 'finish', 'status', 'publish-local', 'online-plan'):
        command = sub.add_parser(name)
        command.add_argument('--run-dir', type=Path, required=True)
        add_summary_arguments(command)
    accept = sub.add_parser('accept-visual')
    accept.add_argument('--run-dir', type=Path, required=True)
    accept.add_argument('--evidence', type=Path, required=True)
    accept.add_argument('--target', choices=('local', 'online'), default='local')
    add_summary_arguments(accept)
    online = sub.add_parser('verify-online')
    online.add_argument('--run-dir', type=Path, required=True)
    online.add_argument('--export', type=Path, required=True)
    online.add_argument('--receipt', type=Path, required=True)
    add_summary_arguments(online)
    inspect = sub.add_parser('inspect', help='Verify sources and locate purchase groups without changing the workbook')
    prepare_parser = sub.add_parser('prepare', help='Create a fresh recoverable run and deterministic append plan')
    for command in (inspect, prepare_parser):
        command.add_argument('--workbook', type=Path, required=True)
        command.add_argument('--recognition', type=Path, required=True)
        command.add_argument('--source-root', type=Path, required=True)
        command.add_argument('--config', type=Path, required=True)
        command.add_argument('--baseline', type=Path)
        command.add_argument('--source-id')
        add_summary_arguments(command)
    inspect.add_argument('--output', type=Path, required=True)
    prepare_parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    if not 512 <= args.max_chars <= 5000:
        parser.error('max-chars must be 512..5000')
    try:
        if args.command not in ('prepare', 'inspect'):
            if args.command == 'status':
                with locked_run(args.run_dir) as (_, state):
                    pass
            elif args.command == 'accept-visual':
                state = accept_visual(args.run_dir, args.evidence, args.target)
            elif args.command == 'verify-online':
                state = verify_online(args.run_dir, args.export, args.receipt)
            else:
                state = {'apply-local': apply_local, 'verify-local': verify_local, 'finish': finish, 'publish-local': publish_local, 'online-plan': online_plan}[args.command](args.run_dir)
            emit_summary({'status': state['stage'], 'local': state['local']['status'],
                          'online': state['online']['status'], 'day_amount': state['plan']['expected_day_amount'],
                          'total_amount': state['plan']['expected_total_amount'],
                          'details_path': str((args.run_dir / 'run.json').resolve())}, args.max_chars)
            return 0
        if args.command == 'prepare':
            state = prepare(args.workbook, args.recognition, args.source_root, args.config,
                            args.run_dir, args.baseline, args.source_id)
            emit_summary({'status': state['stage'], 'details_path': str((args.run_dir / 'run.json').resolve()),
                          'day_amount': state['plan']['expected_day_amount'],
                          'total_amount': state['plan']['expected_total_amount'],
                          'insert_count': state['plan']['insert_count'],
                          'acceptance_verified': False}, args.max_chars)
            return 0
        report = inspect_ledger(args.workbook, args.recognition, args.source_root,
                                args.config, args.baseline, args.source_id)
        path = write_new_json(args.output, report,
                              [args.workbook, args.recognition, args.config, args.baseline])
        counts = {}
        for group in report['groups']:
            counts[group['action']] = counts.get(group['action'], 0) + 1
        emit_summary({'status': 'observed', 'observation_only': True,
                      'recognition_and_acceptance_not_verified': True,
                      'actions': counts, 'groups': report['groups'][:10],
                      'details_path': path, 'group_count': len(report['groups'])}, args.max_chars)
    except (ValueError, OSError, KeyError, TypeError) as error:
        emit_summary({'status': 'failed', 'check_complete': False, 'error_count': 1,
                      'errors': [str(error)[:240]]}, args.max_chars)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
