"""Offline integrity and dependency report; no imports of Django settings or secret reads."""
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys

from .configuration import BASE, PilotError


def verify(root):
    root = Path(root).resolve()
    if (root/'PACKAGE.json').exists():
        from stage_one_demo.package import verify as verify_current
        return verify_current(root)
    manifest = json.loads((root/'PILOT_PACKAGE.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or manifest.get('product_base') != BASE:
        raise PilotError('Неизвестная версия пакета пилота.')
    expected = manifest['files']
    for name, digest in expected.items():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
            raise PilotError('Недопустимый путь в манифесте.')
        target = root.joinpath(*path.parts)
        if any(p.is_symlink() for p in (target, *target.parents)):
            raise PilotError('Ссылки в исходниках недопустимы.')
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise PilotError('Исходники пакета изменены или неполны.')
    actual = set()
    for folder, dirs, names in os.walk(root):
        dirs[:] = [n for n in dirs if n not in ('.venv','.local-state','__pycache__')]
        if any((Path(folder)/name).is_symlink() for name in dirs):
            raise PilotError('Посторонние ссылки в исходниках недопустимы.')
        for name in names:
            if name in ('.DS_Store','Thumbs.db','desktop.ini'):
                continue
            actual.add((Path(folder)/name).relative_to(root).as_posix())
    if actual - {'PILOT_PACKAGE.json','pilot-config.json'} != set(expected):
        raise PilotError('В распаковке есть отсутствующие/посторонние файлы; секретные файлы недопустимы.')
    return manifest


def dependencies(root):
    root = Path(root)
    paths = ['requirements.txt', 'requirements-pilot.txt']
    if sys.platform == 'win32':
        paths.append('requirements-windows.txt')
    mismatches = []
    for path in paths:
        for name, required in re.findall(r'^([A-Za-z0-9_.-]+)==([^\s]+)', (root/path).read_text(), re.M):
            try:
                installed = version(name)
            except PackageNotFoundError:
                installed = None
            if installed != required:
                mismatches.append(name)
    return {'python_3_12':sys.version_info[:2]==(3,12), 'virtual_environment':sys.prefix != sys.base_prefix,
            'missing_or_different_packages':sorted(set(mismatches)), 'platform_verified':False,
            'public_https_verified':False, 'live_telegram_verified':False}
