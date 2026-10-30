"""Build distributables and remove local account metadata from the sdist."""
from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import tomllib


def normalize_sdist(path: Path) -> None:
    descriptor, filename = tempfile.mkstemp(prefix='.normalized-', dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
                with tarfile.open(path, 'r:gz') as source, tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as target:
                    for member in source.getmembers():
                        if not member.isfile() and not member.isdir():
                            raise ValueError('Unexpected non-file entry in source distribution')
                        member.uid = member.gid = 0
                        member.uname = member.gname = ''
                        member.mtime = 0
                        member.mode = 0o755 if member.isdir() else 0o644
                        member.pax_headers = {}
                        if member.isfile():
                            stream = source.extractfile(member)
                            if stream is None:
                                raise ValueError('Missing source distribution member')
                            with stream:
                                target.addfile(member, stream)
                        else:
                            target.addfile(member)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    stem = metadata['name'].replace('-', '_') + '-' + metadata['version']
    dist = root / 'dist'
    subprocess.run(['uv', 'build', '--quiet', '--out-dir', str(dist)], cwd=root, check=True)
    sdist = dist / (stem + '.tar.gz')
    wheel = dist / (stem + '-py3-none-any.whl')
    normalize_sdist(sdist)
    lines = [hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name for path in (sdist, wheel)]
    (dist / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
