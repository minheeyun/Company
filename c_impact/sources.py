"""입력 소스 준비: 디렉터리 비교, Git 커밋 비교, Git 작업 트리, 패치(diff) 적용.

원본 저장소/디렉터리는 읽기만 한다. Git 리비전과 패치 적용 결과는 임시 디렉터리에 만든다.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import tempfile


class SourceError(RuntimeError):
    pass


def _git(repo, *args, binary=False):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True)
    if r.returncode != 0:
        raise SourceError(f"git {' '.join(args)} 실패: {r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def is_git_repo(path) -> bool:
    try:
        return _git(path, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except (SourceError, FileNotFoundError):
        return False


def git_toplevel(path) -> str:
    return _git(path, "rev-parse", "--show-toplevel").strip()


def export_rev(repo: str, rev: str, dest: str, subdir: str = "") -> str:
    """git archive 로 리비전을 dest 에 풀어 놓는다(작업 트리 변경 없음)."""
    data = _git(repo, "archive", "--format=tar", rev, *([subdir] if subdir else []), binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12
            tf.extractall(dest)
    return os.path.join(dest, subdir) if subdir else dest


def changed_files(repo: str, base: str, head: str | None) -> list[str]:
    args = ["diff", "--name-only", base] + ([head] if head else [])
    return [x for x in _git(repo, *args).splitlines() if x.endswith((".c", ".h"))]


def rev_id(repo: str, rev: str) -> str:
    return _git(repo, "rev-parse", "--short", rev).strip()


def apply_patch(base_dir: str, patch_file: str, dest: str) -> str:
    shutil.copytree(base_dir, dest, dirs_exist_ok=True)
    patch_file = os.path.abspath(patch_file)
    for cmd in (["git", "apply", "--unsafe-paths", "--whitespace=nowarn", patch_file],
                ["patch", "-p1", "-i", patch_file]):
        try:
            r = subprocess.run(cmd, cwd=dest, capture_output=True)
        except FileNotFoundError:
            continue
        if r.returncode == 0:
            return dest
    raise SourceError(f"패치 적용 실패: {patch_file}")


def patch_files(patch_file: str) -> list[str]:
    out = []
    with open(patch_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("+++ "):
                p = line[4:].strip().split("\t")[0]
                if p != "/dev/null":
                    out.append(p[2:] if p.startswith(("a/", "b/")) else p)
    return [x for x in out if x.endswith((".c", ".h"))]


class Sources:
    """분석할 Baseline/Current 루트와 메타데이터. with 문으로 임시 디렉터리를 정리한다."""

    def __init__(self, base_root, curr_root, label, tmp=None, target_files=None, meta=None):
        self.base_root = base_root
        self.curr_root = curr_root
        self.label = label
        self._tmp = tmp
        self.target_files = target_files
        self.meta = meta or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.cleanup()

    def cleanup(self):
        if self._tmp and os.path.isdir(self._tmp):
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None


def prepare(args) -> Sources:
    """CLI 인자 → Sources. 지원 모드:
       1) --base-dir/--curr-dir             (Git 이 아닌 경우 포함, 디렉터리 직접 비교)
       2) --repo --base-rev [--head-rev]     (커밋 간 비교)
       3) --repo --base-rev --working-tree   (기준 커밋 대비 작업 트리 변경)
       4) --base-dir --patch file.diff       (Git Diff/패치 적용 결과를 Current 로)
    """
    files = list(args.files or [])
    if getattr(args, "repo", None):
        repo = os.path.abspath(args.repo)
        if not is_git_repo(repo):
            raise SourceError(f"Git 저장소가 아닙니다: {repo}")
        top = git_toplevel(repo)
        sub = os.path.relpath(repo, top).replace(os.sep, "/")
        sub = "" if sub == "." else sub
        tmp = tempfile.mkdtemp(prefix="c_impact_")
        base_rev = args.base_rev or "HEAD"
        base = export_rev(top, base_rev, os.path.join(tmp, "base"), sub)
        if args.working_tree or not args.head_rev:
            curr = repo
            label = f"{rev_id(top, base_rev)}..working-tree"
            head = None
        else:
            curr = export_rev(top, args.head_rev, os.path.join(tmp, "curr"), sub)
            label = f"{rev_id(top, base_rev)}..{rev_id(top, args.head_rev)}"
            head = args.head_rev
        changed = changed_files(top, base_rev, head)
        if sub:
            changed = [c[len(sub) + 1:] for c in changed if c.startswith(sub + "/")]
        return Sources(base, curr, label, tmp, set(files) if files else None,
                       {"mode": "git", "repo": repo, "base_rev": base_rev, "head_rev": head or "WORKING_TREE",
                        "git_changed_files": changed})
    if not getattr(args, "base_dir", None):
        raise SourceError("--base-dir 또는 --repo 가 필요합니다")
    base = os.path.abspath(args.base_dir)
    if not os.path.isdir(base):
        raise SourceError(f"Baseline 디렉터리가 없습니다: {base}")
    if getattr(args, "patch", None):
        tmp = tempfile.mkdtemp(prefix="c_impact_")
        curr = apply_patch(base, args.patch, os.path.join(tmp, "curr"))
        return Sources(base, curr, f"{os.path.basename(base)}+patch", tmp,
                       set(files) if files else None,
                       {"mode": "patch", "patch": os.path.abspath(args.patch), "patch_files": patch_files(args.patch)})
    if not getattr(args, "curr_dir", None):
        raise SourceError("--curr-dir 또는 --patch 가 필요합니다")
    curr = os.path.abspath(args.curr_dir)
    if not os.path.isdir(curr):
        raise SourceError(f"Current 디렉터리가 없습니다: {curr}")
    return Sources(base, curr, f"{os.path.basename(base)}..{os.path.basename(curr)}", None,
                   set(files) if files else None, {"mode": "dirs"})
