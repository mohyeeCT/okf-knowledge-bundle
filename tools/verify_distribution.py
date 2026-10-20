"""Verify the public distribution offline with Python's standard library."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_map(folder):
    return {
        path.relative_to(folder).as_posix(): sha(path.read_bytes())
        for path in folder.rglob('*')
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'
    }


def main():
    manifest = json.loads((ROOT / 'docs/package-manifest-0.2.13-public.1.json').read_text(encoding='utf-8'))
    expected = manifest['files']
    assert len(expected) == 61
    for platform in ('openai', 'claude'):
        folder = ROOT / platform / 'okf-knowledge-bundle'
        assert file_map(folder) == expected, platform
        for name in expected:
            path = PurePosixPath(name)
            assert not path.is_absolute() and '..' not in path.parts and '\\' not in name
        assert '__version__ = "0.2.13"' in (folder / 'scripts/okf_bundle/__init__.py').read_text()
    for archive_name, expected_sha in manifest['archives'].items():
        archive = ROOT / archive_name
        assert sha(archive.read_bytes()) == expected_sha
        with zipfile.ZipFile(archive) as z:
            assert len(z.namelist()) == len(set(z.namelist())) == len(expected)
            assert {name.removeprefix('okf-knowledge-bundle/'): sha(z.read(name)) for name in z.namelist()} == expected
    checked_links = 0
    for path in ROOT.rglob('*.md'):
        if '.git' in path.parts:
            continue
        text = path.read_text(encoding='utf-8')
        assert not re.search(r'C:\\Users\\|/home/claude/', text, re.I), path
        assert not re.search(r'references/review-fixes-|references/spec-v0\.2\.md|benchmarks/', text), path
        assert not re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|jina_[A-Za-z0-9_-]{35,}|sk-[A-Za-z0-9]{30,})\b', text), path
        for target in re.findall(r'(?<!!)\[[^\]\n]+\]\(([^\s)]+)\)', text):
            if target.startswith(('https://', 'http://', '#', 'mailto:')):
                continue
            relative = target.split('#', 1)[0]
            if not relative or any(x in relative for x in ['<', '>', '{', '}']):
                continue
            assert (path.parent / relative).exists(), (str(path.relative_to(ROOT)), target)
            checked_links += 1
    report = {
        'status': 'passed', 'version': '0.2.13', 'public_packaging_revision': 1,
        'platforms': ['claude', 'openai'], 'files_per_skill': len(expected),
        'matching_platform_and_archive_hashes': True,
        'local_markdown_links_checked': checked_links,
        'client_data_or_historical_notes_included': False, 'network_or_provider_calls': 0,
    }
    print(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    main()
