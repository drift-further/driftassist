"""Render a standalone download installer for an explicitly reviewed commit."""
import argparse
import re
import shlex
from pathlib import Path


def render(revision, repository='https://github.com/drift-further/driftassist.git', branch='main'):
    if not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', revision):
        raise ValueError('Use a full reviewed commit, not a branch or tag name.')
    if not repository or not branch or '\n' in repository or '\n' in branch:
        raise ValueError('Repository and branch must be nonempty single-line values.')
    template = Path(__file__).with_name('install.sh.in').read_text()
    values = {'REVISION': revision, 'REPOSITORY': repository, 'BRANCH': branch}
    return re.sub(r'@(REVISION|REPOSITORY|BRANCH)@',
                  lambda match: shlex.quote(values[match[1]]), template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True, help='Full commit reviewed for distribution')
    parser.add_argument('--repository', default='https://github.com/drift-further/driftassist.git')
    parser.add_argument('--branch', default='main', help='Tracking branch used by assist update')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        content = render(args.revision, args.repository, args.branch)
    except ValueError as exc:
        parser.error(str(exc))
    # Do not overwrite an existing artifact; publish only after its review.
    try:
        with args.output.open('x', encoding='utf-8') as stream:
            stream.write(content)
        args.output.chmod(0o700)
    except OSError as exc:
        parser.error(f'Cannot create installer artifact: {exc}')
    print(f'Prepared {args.output} for reviewed revision {args.revision}. No publication performed.')


if __name__ == '__main__':
    main()
