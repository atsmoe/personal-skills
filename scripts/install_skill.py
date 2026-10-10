#!/usr/bin/env python3
"""Install one portable skill into an explicitly chosen discovery directory."""
import argparse
import json
import shutil
from pathlib import Path


def install(skill, destination):
    root = Path(__file__).resolve().parents[1]
    source = root / 'skills' / skill
    available = {p.name for p in (root / 'skills').iterdir() if p.is_dir() and (p / 'SKILL.md').is_file()}
    if skill not in available or source.is_symlink():
        raise ValueError('Select a skill listed in this repository')
    destination = Path(destination).expanduser().resolve()
    target = destination / skill
    if target.exists() or target.is_symlink():
        raise FileExistsError('Existing skill is preserved; compare/update it separately: ' + str(target))
    if target.is_relative_to(root) or root.is_relative_to(target):
        raise ValueError('Install destination must not overlap this repository')
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
    return {'skill': skill, 'path': str(target), 'dependencies_installed': False,
            'agent_discovery_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skill', required=True)
    parser.add_argument('--destination', required=True, type=Path, help='Skill directory scanned by your current agent')
    args = parser.parse_args()
    print(json.dumps(install(args.skill, args.destination), ensure_ascii=False))


if __name__ == '__main__':
    main()
