from __future__ import annotations

# 在任务容器内执行；只克隆当前源码树和子模块当前提交，不拷贝未来历史或改动工作树
ISOLATE_GIT_SCRIPT = r'''
import json
import os
import shutil
import subprocess
import tempfile

root = "/testbed"
if os.path.realpath(os.getcwd()) != root or not os.path.isdir(root + "/.git"):
    raise RuntimeError("Expected the disposable /testbed repository")

# 调用容器自带 Git，所有传输都显式使用本地 file URL
def git(args, cwd=root, data=None):
    return subprocess.check_output(["git"] + args, cwd=cwd, input=data).decode("utf-8").strip()

# 复制索引和影响 Git 规范化的本地设置，保留已跟踪但被忽略的文件及文件模式
def preserve_index(source, target, source_git, worktree=None):
    shutil.copyfile(os.path.join(source_git, "index"), os.path.join(target, ".git", "index"))
    options = ("core.autocrlf", "core.eol", "core.filemode", "core.symlinks", "core.ignorecase")
    for option in options:
        value = subprocess.run(["git", "config", "--local", "--get", option], cwd=source,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if value.returncode == 0:
            git(["config", option, value.stdout.decode("utf-8").strip()], target)
    for name in ("attributes", "exclude"):
        path = os.path.join(source_git, "info", name)
        if os.path.isfile(path):
            shutil.copyfile(path, os.path.join(target, ".git", "info", name))
    git(["remote", "remove", "origin"], target)
    if worktree:
        git(["config", "core.worktree", worktree], target)

git(["add", "-A", "."])
original_tree = git(["write-tree"])
modules = subprocess.check_output([
    "git", "submodule", "foreach", "--recursive", "--quiet", 'printf "%s\\0" "$displaypath"'
], cwd=root).split(b"\0")
temporary = tempfile.mkdtemp(prefix="loopsmith-git-", dir="/root")
clean = os.path.join(temporary, "main")
snapshot = git(["-c", "user.name=LoopSmith", "-c", "user.email=evaluation@localhost",
                "commit-tree", original_tree], data=b"source-snapshot\n")
git(["update-ref", "refs/heads/loopsmith-source-snapshot", snapshot])
git(["-c", "protocol.file.allow=always", "clone", "-q", "--no-checkout", "--depth=1",
     "--single-branch", "--branch", "loopsmith-source-snapshot", "file://" + root, clean])
preserve_index(root, clean, root + "/.git")
module_count = 0
for raw_path in modules:
    if not raw_path:
        continue
    path = raw_path.decode("utf-8")
    source = os.path.realpath(os.path.join(root, path))
    if not source.startswith(root + "/"):
        raise RuntimeError("Submodule outside the disposable repository")
    old_git = os.path.realpath(git(["rev-parse", "--absolute-git-dir"], source))
    relative_git = os.path.relpath(old_git, root + "/.git")
    if relative_git.startswith(".."):
        raise RuntimeError("Unexpected external submodule Git directory")
    git(["update-ref", "refs/heads/loopsmith-source-snapshot", "HEAD"], source)
    target = os.path.join(temporary, "module-" + str(module_count))
    git(["-c", "protocol.file.allow=always", "clone", "-q", "--no-checkout", "--depth=1",
         "--single-branch", "--branch", "loopsmith-source-snapshot", "file://" + source, target])
    preserve_index(source, target, old_git, source)
    destination = os.path.join(clean, ".git", relative_git)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.move(os.path.join(target, ".git"), destination)
    module_count += 1
shutil.rmtree(root + "/.git")
shutil.move(os.path.join(clean, ".git"), root + "/.git")
shutil.rmtree(temporary)
git(["add", "-A", "."])
if git(["write-tree"]) != original_tree:
    raise RuntimeError("Source tree changed while isolating Git history")
if git(["rev-list", "--all", "--count"]) != "1":
    raise RuntimeError("Unexpected history in the isolated main repository")
print(json.dumps({"source_tree_preserved": True, "isolated_submodules": module_count}))
'''
