"""Narrow Windows-WSL execution support for managed NVIDIA GPU miners.

The controller can select this backend, but it cannot supply a distribution,
Linux command, shell fragment, working directory, or helper path.  The Agent
uses the active console user's default WSL distribution and translates only
the already-verified package/runtime paths that it generated locally.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Sequence

from .fsutil import is_link_or_reparse as _link_or_reparse
from .windows_active_user import ActiveUserIdentity
from .windows_active_user import WindowsActiveUserError
from .windows_active_user import active_user_identity
from .windows_active_user import popen as active_user_popen
from .windows_active_user import run as active_user_run


WSL_GPU_MINING_CAPABILITY = "managed_mining.wsl_gpu.v1"
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_WSL_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_WSL_DISTRIBUTION = re.compile(r"^[A-Za-z0-9._+ -]{1,128}$")
_PACKAGE_MARKER_NAME = ".rigpilot-package.json"
_MAX_CACHE_FILES = 20_000
_MAX_CACHE_BYTES = 64 * 1024 * 1024 * 1024

_GUEST_IDENTITY = r'''import json,os
print(json.dumps({"distribution":os.environ.get("WSL_DISTRO_NAME",""),"uid":os.getuid()},separators=(",",":")))
'''

# Copy one already verified managed GPU-miner package into a private ext4
# cache.  The source root, content digest and executable-relative path are all
# derived locally by the Agent.  This avoids depending on DrvFS executable-bit
# emulation while retaining the package's sibling resources and libraries.
_GUEST_CACHE_PACKAGE = r'''import hashlib,json,os,pwd,re,shutil,stat,sys,tempfile
src=sys.argv[1]; rel=sys.argv[2]; key=sys.argv[3]; expected_count=int(sys.argv[4]); expected_bytes=int(sys.argv[5]); uid=int(sys.argv[6]); distro=sys.argv[7]
if os.getuid()!=uid or os.environ.get("WSL_DISTRO_NAME","")!=distro or not os.path.isabs(src) or os.path.isabs(rel) or not rel or not re.fullmatch(r"[0-9a-f]{64}",key) or expected_count<1 or expected_count>20000 or expected_bytes<1 or expected_bytes>68719476736: raise SystemExit(64)
parts=rel.split("/")
if any(not p or p in (".","..") or "\x00" in p or "\n" in p or "\r" in p for p in parts): raise SystemExit(64)
home=os.path.realpath(pwd.getpwuid(uid).pw_dir)
def secure_dir(path,create=False):
 if create:
  try: os.mkdir(path,0o700)
  except FileExistsError: pass
 st=os.lstat(path)
 if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid!=uid: raise SystemExit(65)
 os.chmod(path,0o700)
 return path
home_stat=os.lstat(home)
if not stat.S_ISDIR(home_stat.st_mode) or stat.S_ISLNK(home_stat.st_mode) or home_stat.st_uid!=uid: raise SystemExit(65)
rig=secure_dir(os.path.join(home,".rigpilot"),True)
base=secure_dir(os.path.join(rig,"miner-cache-v1"),True)
final=os.path.join(base,key)
if os.path.commonpath((base,final))!=base or os.path.dirname(final)!=base: raise SystemExit(65)
marker=".rigpilot-cache.json"; source_marker=".rigpilot-package.json"; executable_names={"miner","miner-cuda12","miner-cuda13","SRBMiner-MULTI"}
def scan(root,copy_root=None):
 stack=[root]; dirs=[]; files=[]; seen=0
 while stack:
  directory=stack.pop()
  with os.scandir(directory) as entries: ordered=sorted(entries,key=lambda e:e.name)
  for entry in ordered:
   relative=os.path.relpath(entry.path,root).replace(os.sep,"/")
   if relative in (source_marker,marker): continue
   seen+=1
   if seen>20000: raise ValueError("entries")
   info=entry.stat(follow_symlinks=False)
   if stat.S_ISLNK(info.st_mode): raise ValueError("link")
   if copy_root is None and (info.st_uid!=uid or (info.st_mode&0o022)!=0): raise ValueError("mode")
   if stat.S_ISDIR(info.st_mode): dirs.append(relative); stack.append(entry.path)
   elif stat.S_ISREG(info.st_mode): files.append((relative,entry.path,info.st_size))
   else: raise ValueError("special")
 if copy_root is not None:
  for relative in sorted(dirs,key=lambda p:(p.count("/"),p)):
   destination=os.path.join(copy_root,*relative.split("/")); os.mkdir(destination,0o700)
 digest=hashlib.sha256(); total=0
 for relative,path,size in sorted(files):
  if size<0 or total+size>68719476736: raise ValueError("size")
  encoded=relative.encode("utf-8"); file_digest=hashlib.sha256(); actual=0
  srcfd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
  try:
   before=os.fstat(srcfd)
   if not stat.S_ISREG(before.st_mode) or before.st_size!=size: raise ValueError("changed")
   outfd=-1
   if copy_root is not None:
    destination=os.path.join(copy_root,*relative.split("/"))
    mode=0o500 if os.path.basename(relative) in executable_names else 0o400
    outfd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),mode)
   try:
    while True:
     chunk=os.read(srcfd,65536)
     if not chunk: break
     actual+=len(chunk)
     if total+actual>68719476736: raise ValueError("size")
     file_digest.update(chunk)
     if outfd>=0:
      view=memoryview(chunk)
      while view:
       written=os.write(outfd,view)
       if written<=0: raise OSError("short write")
       view=view[written:]
    after=os.fstat(srcfd)
    if actual!=size or after.st_size!=size or after.st_ino!=before.st_ino: raise ValueError("changed")
    if outfd>=0: os.fsync(outfd)
   finally:
    if outfd>=0: os.close(outfd)
  finally: os.close(srcfd)
  total+=actual
  digest.update(len(encoded).to_bytes(4,"big")); digest.update(encoded); digest.update(actual.to_bytes(8,"big")); digest.update(file_digest.digest())
 return digest.hexdigest(),len(files),total,{item[0] for item in files}
def validate(path):
 st=os.lstat(path)
 if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid!=uid or (st.st_mode&0o077)!=0: return False
 try:
  marker_path=os.path.join(path,marker); marker_stat=os.lstat(marker_path)
  if not stat.S_ISREG(marker_stat.st_mode) or stat.S_ISLNK(marker_stat.st_mode) or marker_stat.st_uid!=uid or (marker_stat.st_mode&0o077)!=0 or marker_stat.st_size>4096: return False
  with open(marker_path,"r",encoding="utf-8") as stream: data=json.load(stream)
  if data!={"version":1,"key":key,"file_count":expected_count,"extracted_bytes":expected_bytes}: return False
  found,count,total,files=scan(path)
  target=os.path.join(path,*parts); target_stat=os.lstat(target)
  return found==key and count==expected_count and total==expected_bytes and rel in files and stat.S_ISREG(target_stat.st_mode) and not stat.S_ISLNK(target_stat.st_mode) and target_stat.st_uid==uid and (target_stat.st_mode&0o111)!=0
 except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError): return False
try:
 if not os.path.lexists(final):
  stage=tempfile.mkdtemp(prefix=".tmp-"+key[:16]+"-",dir=base); os.chmod(stage,0o700)
  try:
   found,count,total,files=scan(src,stage)
   if found!=key or count!=expected_count or total!=expected_bytes or rel not in files: raise SystemExit(66)
   manifest={"version":1,"key":key,"file_count":expected_count,"extracted_bytes":expected_bytes}
   mfd=os.open(os.path.join(stage,marker),os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o400)
   try: os.write(mfd,(json.dumps(manifest,separators=(",",":"))+"\n").encode("utf-8")); os.fsync(mfd)
   finally: os.close(mfd)
   try: os.rename(stage,final); stage=None
   except FileExistsError:
    if not validate(final): raise SystemExit(65)
  finally:
   if stage is not None and os.path.lexists(stage):
    if not shutil.rmtree.avoids_symlink_attacks: raise SystemExit(65)
    shutil.rmtree(stage)
 if not validate(final): raise SystemExit(65)
 target=os.path.join(final,*parts); cwd=os.path.dirname(target)
 print(json.dumps({"target":target,"cwd":cwd},separators=(",",":")))
except (FileNotFoundError,NotADirectoryError,PermissionError,OSError,ValueError,KeyError): raise SystemExit(65)
'''

# These programs are Agent source code, not controller input.  They are passed
# to the active user's fixed ``/usr/bin/python3`` executable as one argv item;
# no shell, profile, command substitution, distribution name, or remote text is
# involved.  The supervisor owns exactly one child process group and publishes
# an O_EXCL/0600 token record in /tmp.  A later fixed control program validates
# the token, target, process start ticks, parent/session relationship and
# command line before it can signal that exact group.
_GUEST_SUPERVISOR = r'''import json,os,re,signal,subprocess,sys,time
tok=sys.argv[1]; cwd=sys.argv[2]; uid=int(sys.argv[3]); distro=sys.argv[4]; target=sys.argv[5]; args=sys.argv[5:]
if not re.fullmatch(r"[0-9a-f]{64}",tok) or os.getuid()!=uid or os.environ.get("WSL_DISTRO_NAME","")!=distro or not os.path.isabs(cwd) or not os.path.isabs(target) or not args or args[0]!=target: raise SystemExit(64)
state="/tmp/rigpilot-miner-"+tok+".json"
flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)
fd=os.open(state,flags,0o600); inode=os.fstat(fd).st_ino
child=None; stopping=False; rc=1
def ticks(pid):
 data=open("/proc/%d/stat"%pid,"rb").read().decode("ascii","strict"); return int(data[data.rfind(")")+2:].split()[19])
def request_stop(signum,frame):
 global stopping; stopping=True
def stop_child():
 if child is None: return None
 if child.poll() is None:
  try: os.killpg(child.pid,signal.SIGTERM)
  except ProcessLookupError: pass
  try: return child.wait(timeout=10)
  except subprocess.TimeoutExpired:
   try: os.killpg(child.pid,signal.SIGKILL)
   except ProcessLookupError: pass
 return child.wait(timeout=5)
signal.signal(signal.SIGTERM,request_stop); signal.signal(signal.SIGINT,request_stop)
try:
 env=os.environ.copy(); env["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
 child=subprocess.Popen(args,cwd=cwd,stdin=subprocess.DEVNULL,stdout=None,stderr=None,close_fds=True,start_new_session=True,env=env)
 record={"version":1,"token":tok,"supervisor_pid":os.getpid(),"supervisor_start":ticks(os.getpid()),"child_pid":child.pid,"child_start":ticks(child.pid),"target":target}
 payload=memoryview((json.dumps(record,separators=(",",":"))+"\n").encode("utf-8"))
 while payload:
  written=os.write(fd,payload)
  if written<=0: raise OSError("short record write")
  payload=payload[written:]
 os.fsync(fd); os.close(fd); fd=-1
 while child.poll() is None and not stopping: time.sleep(.2)
 rc=stop_child() if stopping else child.wait()
except BaseException:
 try: stop_child()
 except BaseException: pass
 raise
finally:
 if fd>=0: os.close(fd)
 try:
  st=os.lstat(state)
  if (child is None or child.poll() is not None) and st.st_ino==inode and not os.path.islink(state): os.unlink(state)
 except FileNotFoundError: pass
raise SystemExit(0 if stopping else (rc if isinstance(rc,int) and 0<=rc<=255 else 1))
'''

_GUEST_CONTROL = r'''import json,os,re,signal,sys,time
op=sys.argv[1]; tok=sys.argv[2]; expected=sys.argv[3]; uid=int(sys.argv[4]); distro=sys.argv[5]
if op not in ("query","stop") or not re.fullmatch(r"[0-9a-f]{64}",tok) or not os.path.isabs(expected) or os.getuid()!=uid or os.environ.get("WSL_DISTRO_NAME","")!=distro: raise SystemExit(64)
path="/tmp/rigpilot-miner-"+tok+".json"
def proc(pid,start,target=None,parent=None,session=None):
 try:
  raw=open("/proc/%d/stat"%pid,"rb").read().decode("ascii","strict"); fields=raw[raw.rfind(")")+2:].split()
  if int(fields[19])!=start: return False
  if parent is not None and int(fields[1])!=parent: return False
  if session is not None and int(fields[3])!=session: return False
  if target is not None:
   argv=open("/proc/%d/cmdline"%pid,"rb").read().split(b"\0")
   if not argv or os.fsdecode(argv[0])!=target: return False
  return True
 except (FileNotFoundError,ProcessLookupError,ValueError,OSError): return False
try:
 st=os.lstat(path)
 if os.path.islink(path) or not (st.st_mode&0o170000)==0o100000 or st.st_uid!=os.getuid() or (st.st_mode&0o077)!=0: raise SystemExit(65)
 data=open(path,"r",encoding="utf-8").read(4097)
 if len(data)>4096: raise SystemExit(65)
 r=json.loads(data)
 if set(r)!={"version","token","supervisor_pid","supervisor_start","child_pid","child_start","target"} or r["version"]!=1 or r["token"]!=tok or r["target"]!=expected: raise SystemExit(65)
 sp=int(r["supervisor_pid"]); ss=int(r["supervisor_start"]); cp=int(r["child_pid"]); cs=int(r["child_start"])
 if min(sp,ss,cp,cs)<=0: raise SystemExit(65)
 alive_supervisor=proc(sp,ss)
 if not proc(cp,cs,expected,sp if alive_supervisor else None,cp): raise SystemExit(3)
 if op=="query": raise SystemExit(0)
 if alive_supervisor:
  try: os.kill(sp,signal.SIGTERM)
  except ProcessLookupError: pass
 else:
  try: os.killpg(cp,signal.SIGTERM)
  except ProcessLookupError: pass
 deadline=time.monotonic()+12
 while time.monotonic()<deadline:
  if not proc(cp,cs): raise SystemExit(0)
  time.sleep(.1)
 try: os.killpg(cp,signal.SIGKILL)
 except ProcessLookupError: pass
 deadline=time.monotonic()+5
 while time.monotonic()<deadline:
  if not proc(cp,cs): raise SystemExit(0)
  time.sleep(.1)
 raise SystemExit(67)
except FileNotFoundError: raise SystemExit(3)
except (json.JSONDecodeError,KeyError,TypeError,ValueError): raise SystemExit(65)
'''

_GUEST_PTY_DISCOVERY = r'''import errno,os,pty,select,signal,subprocess,sys,time
uid=int(sys.argv[1]); distro=sys.argv[2]; target=sys.argv[3]
if len(sys.argv)!=5 or sys.argv[4]!="--list-devices" or os.getuid()!=uid or os.environ.get("WSL_DISTRO_NAME","")!=distro or not os.path.isabs(target): raise SystemExit(64)
master,slave=pty.openpty(); child=None; chunks=[]; size=0; deadline=time.monotonic()+14
try:
 env=os.environ.copy(); env["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"
 child=subprocess.Popen([target,"--list-devices"],cwd=os.path.dirname(target),stdin=slave,stdout=slave,stderr=slave,close_fds=True,start_new_session=True,env=env); os.close(slave); slave=-1
 while True:
  running=child.poll() is None
  if running and time.monotonic()>=deadline: raise TimeoutError
  ready,_,_=select.select([master],[],[],.1 if running else 0)
  if ready:
   try: part=os.read(master,8192)
   except OSError as exc:
    if exc.errno==errno.EIO: break
    raise
   if not part: break
   size+=len(part)
   if size>262144: raise ValueError
   chunks.append(part)
  elif not running: break
 rc=child.wait(timeout=max(.1,deadline-time.monotonic()))
 os.write(1,b"".join(chunks)); raise SystemExit(rc)
except (TimeoutError,subprocess.TimeoutExpired,ValueError):
 if child is not None and child.poll() is None:
  try: os.killpg(child.pid,signal.SIGTERM)
  except ProcessLookupError: pass
  try: child.wait(timeout=1)
  except subprocess.TimeoutExpired:
   try: os.killpg(child.pid,signal.SIGKILL)
   except ProcessLookupError: pass
 raise SystemExit(124)
finally:
 for fd in (master,slave):
  if fd>=0:
   try: os.close(fd)
   except OSError: pass
'''


class WslMiningError(RuntimeError):
    """The local active-user WSL GPU runtime is unavailable or unsafe."""


@dataclass(frozen=True)
class WslLaunchIdentity:
    """Local user/distribution identity to which one launch is bound."""

    windows_session_id: int
    windows_user_sid: str
    distribution: str
    linux_uid: int


@dataclass(frozen=True)
class _PackageCacheSpec:
    key: str
    relative_executable: str
    file_count: int
    extracted_bytes: int




def _package_cache_spec(package_root: Path, target: Path) -> _PackageCacheSpec:
    """Read the immutable install marker used to bind one guest cache copy."""

    try:
        root = package_root.resolve(strict=True)
        executable = target.resolve(strict=True)
        root_info = root.lstat()
        executable_info = executable.lstat()
        marker = root / _PACKAGE_MARKER_NAME
        marker_info = marker.lstat()
    except OSError as exc:
        raise WslMiningError("WSL miner package cannot be inspected") from exc
    if _link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise WslMiningError("WSL miner package root is unsafe")
    if _link_or_reparse(executable_info) or not stat.S_ISREG(executable_info.st_mode):
        raise WslMiningError("WSL miner executable is unsafe")
    if (
        _link_or_reparse(marker_info)
        or not stat.S_ISREG(marker_info.st_mode)
        or marker_info.st_size <= 0
        or marker_info.st_size > 64 * 1024
    ):
        raise WslMiningError("WSL miner package marker is unsafe")
    try:
        relative_path = executable.relative_to(root)
    except ValueError as exc:
        raise WslMiningError("WSL miner executable is outside its package") from exc
    relative = relative_path.as_posix()
    pure = PurePosixPath(relative)
    if (
        not relative
        or len(relative) > 1024
        or len(pure.parts) > 64
        or pure.is_absolute()
        or any(
            part in {"", ".", ".."}
            or any(ord(character) < 32 for character in part)
            for part in pure.parts
        )
    ):
        raise WslMiningError("WSL miner package entry path is unsafe")
    try:
        marker_value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WslMiningError("WSL miner package marker is invalid") from exc
    if not isinstance(marker_value, dict):
        raise WslMiningError("WSL miner package marker is invalid")
    key = marker_value.get("tree_sha256")
    file_count = marker_value.get("file_count")
    extracted_bytes = marker_value.get("extracted_bytes")
    if (
        not isinstance(key, str)
        or re.fullmatch(r"[0-9a-f]{64}", key) is None
        or isinstance(file_count, bool)
        or not isinstance(file_count, int)
        or not 1 <= file_count <= _MAX_CACHE_FILES
        or isinstance(extracted_bytes, bool)
        or not isinstance(extracted_bytes, int)
        or not 1 <= extracted_bytes <= _MAX_CACHE_BYTES
    ):
        raise WslMiningError("WSL miner package marker has invalid cache metadata")
    return _PackageCacheSpec(key, relative, file_count, extracted_bytes)


def _windows_system_executable(name: str) -> Path:
    """Resolve one fixed System32 executable without trusting environment data."""

    if platform.system() != "Windows":
        raise WslMiningError("WSL managed mining is available only on Windows")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.exe", name):
        raise WslMiningError("trusted Windows executable name is invalid")
    buffer = ctypes.create_unicode_buffer(32768)
    try:
        length = int(ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer)))
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise WslMiningError("Windows System32 directory cannot be resolved") from exc
    if length <= 0 or length >= len(buffer):
        raise WslMiningError("Windows System32 directory cannot be resolved")
    path = Path(buffer.value) / name
    try:
        if not path.is_file():
            raise WslMiningError(
                "Windows WSL is not installed; enable WSL2 and a default Linux "
                "distribution, then retry"
            )
        return path.resolve(strict=True)
    except OSError as exc:
        raise WslMiningError(
            "Windows WSL is not installed; enable WSL2 and a default Linux "
            "distribution, then retry"
        ) from exc


def wsl_executable_path() -> Path:
    """Return the trusted native System32 WSL launcher path."""

    return _windows_system_executable("wsl.exe")


def ensure_wsl_package_read_access(
    package_root: Path,
    *,
    packages_root: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Grant builtin Users narrowly scoped access to one verified package.

    The Agent data directory remains SYSTEM/Administrators-only.  WSL runs as
    the interactive user, so it needs RX on the packages directory itself to
    traverse into the selected package, then inherited RX within only that
    immutable package tree.  It never receives write access to configuration,
    state, runtime metadata, logs, or sibling packages.
    """

    def real_directory(value: Path, label: str) -> Path:
        try:
            info = value.lstat()
            resolved = value.resolve(strict=True)
            resolved_info = resolved.lstat()
        except OSError as exc:
            raise WslMiningError(f"WSL {label} directory is unavailable") from exc
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0) & reparse)
            or not stat.S_ISDIR(resolved_info.st_mode)
            or stat.S_ISLNK(resolved_info.st_mode)
            or bool(getattr(resolved_info, "st_file_attributes", 0) & reparse)
        ):
            raise WslMiningError(f"WSL {label} must be a real local directory")
        return resolved

    trusted_packages = real_directory(packages_root, "packages root")
    root = real_directory(package_root, "package")
    if root.parent != trusted_packages:
        raise WslMiningError(
            "WSL package must be a direct child of the configured packages directory"
        )
    try:
        icacls = _windows_system_executable("icacls.exe")
        commands = (
            [
                str(icacls),
                str(trusted_packages),
                "/grant:r",
                "*S-1-5-32-545:(RX)",
                "/C",
                "/Q",
            ],
            [
                str(icacls),
                str(root),
                "/grant:r",
                "*S-1-5-32-545:(OI)(CI)(RX)",
                "/T",
                "/C",
                "/Q",
            ],
        )
        for command in commands:
            result = runner(
                command,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
            if result.returncode != 0:
                raise WslMiningError(
                    "WSL package read permission could not be prepared"
                )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WslMiningError("WSL package read permission could not be prepared") from exc


def _bounded_linux_path(value: str, label: str) -> str:
    path = value.strip().replace("\r", "").replace("\n", "")
    pure = PurePosixPath(path)
    if (
        not path
        or len(path) > 4096
        or not pure.is_absolute()
        or pure.as_posix() != path
        or "\\" in path
        or any(ord(character) < 32 for character in path)
        or any(part in {"", ".", ".."} for part in pure.parts[1:])
    ):
        raise WslMiningError(f"WSL {label} path is invalid")
    return path


def wsl_cached_executable_matches(value: str, key: str) -> bool:
    """Return whether a guest target is inside the fixed digest cache root."""

    if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{64}", key) is None:
        return False
    try:
        path = _bounded_linux_path(value, "cached executable")
    except (TypeError, AttributeError, WslMiningError):
        return False
    parts = PurePosixPath(path).parts
    matches = [
        index
        for index, part in enumerate(parts)
        if part == ".rigpilot"
        and index + 2 < len(parts)
        and parts[index + 1] == "miner-cache-v1"
        and parts[index + 2] == key
    ]
    return len(matches) == 1 and matches[0] + 3 < len(parts)


def _run_checked(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    argv: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    failure: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(argv, timeout=timeout)
    except (WindowsActiveUserError, OSError, subprocess.TimeoutExpired) as exc:
        raise WslMiningError(failure) from exc
    if result.returncode != 0:
        raise WslMiningError(f"{failure} (exit {result.returncode})")
    return result


def _prepare_guest_package_cache(
    wsl_executable: Path,
    identity: WslLaunchIdentity,
    source_root: str,
    spec: _PackageCacheSpec,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, str]:
    """Create/reuse one content-bound private ext4 package cache."""

    command = [
        str(wsl_executable),
        "--distribution",
        identity.distribution,
        "--exec",
        "/usr/bin/python3",
        "-c",
        _GUEST_CACHE_PACKAGE,
        _bounded_linux_path(source_root, "package source"),
        spec.relative_executable,
        spec.key,
        str(spec.file_count),
        str(spec.extracted_bytes),
        str(identity.linux_uid),
        identity.distribution,
    ]
    result = _run_checked(
        runner,
        command,
        timeout=180,
        failure="WSL miner package could not be copied into the private Linux cache",
    )
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or set(value) != {"target", "cwd"}:
            raise ValueError
        target = _bounded_linux_path(value["target"], "cached executable")
        cwd = _bounded_linux_path(value["cwd"], "cached working directory")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise WslMiningError("WSL miner package cache returned an invalid path") from exc
    suffix = (
        f"/.rigpilot/miner-cache-v1/{spec.key}/"
        f"{spec.relative_executable}"
    )
    if (
        not wsl_cached_executable_matches(target, spec.key)
        or not target.endswith(suffix)
        or str(PurePosixPath(target).parent) != cwd
    ):
        raise WslMiningError("WSL miner package cache escaped its fixed boundary")
    return target, cwd


def new_wsl_supervisor_token() -> str:
    """Return one unpredictable local launch identity for a guest miner."""

    return secrets.token_hex(32)


def _checked_token(value: str) -> str:
    if not isinstance(value, str) or _WSL_TOKEN.fullmatch(value) is None:
        raise WslMiningError("WSL miner supervisor token is invalid")
    return value


def _guest_control_command(
    wsl_executable: Path,
    operation: str,
    token: str,
    guest_executable: str,
    identity: WslLaunchIdentity,
) -> list[str]:
    if operation not in {"query", "stop"}:
        raise WslMiningError("WSL miner control operation is invalid")
    return [
        str(wsl_executable),
        "--distribution",
        identity.distribution,
        "--exec",
        "/usr/bin/python3",
        "-c",
        _GUEST_CONTROL,
        operation,
        _checked_token(token),
        _bounded_linux_path(guest_executable, "guest executable"),
        str(identity.linux_uid),
        identity.distribution,
    ]


def _assert_active_identity(
    expected: WslLaunchIdentity,
    identity_factory: Callable[[], ActiveUserIdentity],
) -> None:
    try:
        current = identity_factory()
    except WindowsActiveUserError as exc:
        raise WslMiningError(str(exc)) from exc
    if (
        current.session_id != expected.windows_session_id
        or current.user_sid.casefold() != expected.windows_user_sid.casefold()
    ):
        raise WslMiningError(
            "active Windows user/session changed since the WSL miner was started"
        )


def query_wsl_guest(
    wsl_executable: Path,
    token: str,
    guest_executable: str,
    identity: WslLaunchIdentity,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = active_user_run,
    identity_factory: Callable[[], ActiveUserIdentity] = active_user_identity,
) -> bool:
    """Prove whether the token-bound supervisor and exact child are alive."""

    _assert_active_identity(identity, identity_factory)
    command = _guest_control_command(
        wsl_executable, "query", token, guest_executable, identity
    )
    try:
        result = runner(command, timeout=10)
    except (WindowsActiveUserError, OSError, subprocess.TimeoutExpired) as exc:
        raise WslMiningError("WSL miner supervisor could not be inspected") from exc
    if result.returncode == 0:
        _assert_active_identity(identity, identity_factory)
        return True
    if result.returncode == 3:
        _assert_active_identity(identity, identity_factory)
        return False
    raise WslMiningError("WSL miner supervisor identity is invalid")


def wait_wsl_guest_ready(
    wsl_executable: Path,
    token: str,
    guest_executable: str,
    identity: WslLaunchIdentity,
    *,
    timeout: float = 15.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = active_user_run,
    identity_factory: Callable[[], ActiveUserIdentity] = active_user_identity,
) -> None:
    """Wait until the fixed guest record proves the exact miner is running."""

    deadline = time.monotonic() + timeout
    while True:
        if query_wsl_guest(
            wsl_executable,
            token,
            guest_executable,
            identity,
            runner=runner,
            identity_factory=identity_factory,
        ):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WslMiningError(
                "WSL miner supervisor did not become ready before timeout"
            )
        time.sleep(min(0.2, remaining))


def stop_wsl_guest(
    wsl_executable: Path,
    token: str,
    guest_executable: str,
    identity: WslLaunchIdentity,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = active_user_run,
    identity_factory: Callable[[], ActiveUserIdentity] = active_user_identity,
) -> bool:
    """Stop and confirm one token-bound guest miner, never a distribution."""

    _assert_active_identity(identity, identity_factory)
    command = _guest_control_command(
        wsl_executable, "stop", token, guest_executable, identity
    )
    try:
        result = runner(command, timeout=20)
    except (WindowsActiveUserError, OSError, subprocess.TimeoutExpired) as exc:
        raise WslMiningError("WSL miner guest process could not be stopped") from exc
    if result.returncode == 0:
        # A second independent query proves the control process did not merely
        # return after sending a signal.
        if query_wsl_guest(
            wsl_executable,
            token,
            guest_executable,
            identity,
            runner=runner,
            identity_factory=identity_factory,
        ):
            raise WslMiningError("WSL miner guest process remained alive after stop")
        return True
    if result.returncode == 3:
        return False
    raise WslMiningError("WSL miner guest process identity is invalid")


@dataclass
class WslMiningRuntime:
    """A preflighted, active-user default WSL runtime for one package binary."""

    wsl_executable: Path
    target_executable: Path
    linux_executable: str
    linux_cwd: str
    identity: WslLaunchIdentity
    cache_key: str
    runner: Callable[..., subprocess.CompletedProcess[str]] = field(repr=False)
    popen_factory: Callable[..., Any] = field(repr=False)
    _translations: dict[str, str] = field(default_factory=dict, repr=False)

    def translate_path(self, value: str | os.PathLike[str]) -> str:
        raw = os.fspath(value)
        cached = self._translations.get(raw)
        if cached is not None:
            return cached
        result = _run_checked(
            self.runner,
            [
                str(self.wsl_executable),
                "--distribution",
                self.identity.distribution,
                "--exec",
                "/usr/bin/wslpath",
                "-a",
                "-u",
                raw,
            ],
            timeout=10,
            failure=(
                "Windows WSL could not translate an Agent-owned miner path; "
                "verify the default WSL2 distribution"
            ),
        )
        translated = _bounded_linux_path(result.stdout, "translated")
        self._translations[raw] = translated
        return translated

    def command(self, argv: Sequence[str | os.PathLike[str]]) -> list[str]:
        if not argv or Path(os.fspath(argv[0])).resolve() != self.target_executable:
            raise WslMiningError("WSL miner argv does not start with the verified executable")
        translated: list[str] = [self.linux_executable]
        for item in argv[1:]:
            value = os.fspath(item)
            # Pool, wallet, worker and miner options can contain arbitrary
            # controller-supplied strings.  Never reinterpret a value that
            # merely *looks* like a Windows path and disclose/translate it
            # through WSL.  This first GPU-only backend needs no other host
            # paths after its Agent-owned executable and cwd are translated.
            if _WINDOWS_ABSOLUTE_PATH.match(value):
                raise WslMiningError(
                    "WSL miner option contains an unsupported Windows path"
                )
            translated.append(value)
        return [
            str(self.wsl_executable),
            "--distribution",
            self.identity.distribution,
            "--cd",
            self.linux_cwd,
            "--exec",
            *translated,
        ]

    def supervised_command(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        token: str,
    ) -> list[str]:
        """Build the fixed supervisor invocation for one verified miner argv."""

        translated = self.command(argv)
        # ``command`` has already translated and validated every Agent-owned
        # Windows path.  Replace its direct --exec target with the fixed Python
        # supervisor; the miner argv remains separate argv elements.
        guest_argv = translated[translated.index("--exec") + 1 :]
        if not guest_argv or guest_argv[0] != self.linux_executable:
            raise WslMiningError("WSL translated miner argv is invalid")
        return [
            str(self.wsl_executable),
            "--distribution",
            self.identity.distribution,
            "--cd",
            self.linux_cwd,
            "--exec",
            "/usr/bin/python3",
            "-c",
            _GUEST_SUPERVISOR,
            _checked_token(token),
            self.linux_cwd,
            str(self.identity.linux_uid),
            self.identity.distribution,
            *guest_argv,
        ]

    def run_linux(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        timeout: float | None = None,
        **_ignored: Any,
    ) -> subprocess.CompletedProcess[str]:
        # This callable is intentionally narrow: it is used only for the
        # SRBMiner ``--list-devices`` probe.  A fixed Python PTY bridge is
        # necessary because SRBMiner suppresses its Linux device table when
        # stdout is a pipe.  Neither the controller nor a flight sheet can
        # select another guest command.
        if (
            len(argv) != 2
            or Path(os.fspath(argv[0])).resolve() != self.target_executable
            or os.fspath(argv[1]) != "--list-devices"
        ):
            raise WslMiningError("WSL SRBMiner discovery command is invalid")
        command = [
            str(self.wsl_executable),
            "--distribution",
            self.identity.distribution,
            "--cd",
            self.linux_cwd,
            "--exec",
            "/usr/bin/python3",
            "-c",
            _GUEST_PTY_DISCOVERY,
            str(self.identity.linux_uid),
            self.identity.distribution,
            self.linux_executable,
            "--list-devices",
        ]
        return self.runner(command, timeout=timeout)

    def popen(self, argv: Sequence[str | os.PathLike[str]], *, stdout: Any) -> Any:
        return self.popen_factory(self.command(argv), stdout=stdout)


def prepare_wsl_gpu_runtime(
    target_executable: Path,
    *,
    package_root: Path,
    system_name: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = active_user_run,
    popen_factory: Callable[..., Any] = active_user_popen,
    identity_factory: Callable[[], ActiveUserIdentity] = active_user_identity,
) -> WslMiningRuntime:
    """Preflight the active user's default WSL2 NVIDIA mapping before mutation."""

    if (system_name or platform.system()) != "Windows":
        raise WslMiningError("WSL GPU mining is available only on Windows devices")
    try:
        target = target_executable.resolve(strict=True)
    except OSError as exc:
        raise WslMiningError("WSL miner executable is missing") from exc
    cache_spec = _package_cache_spec(package_root, target)
    try:
        verified_package_root = package_root.resolve(strict=True)
    except OSError as exc:
        raise WslMiningError("WSL miner package is unavailable") from exc
    wsl = wsl_executable_path()
    try:
        host_identity = identity_factory()
    except WindowsActiveUserError as exc:
        raise WslMiningError(str(exc)) from exc
    identity_result = _run_checked(
        runner,
        [str(wsl), "--exec", "/usr/bin/python3", "-c", _GUEST_IDENTITY],
        timeout=10,
        failure=(
            "Windows WSL default distribution identity could not be determined; "
            "install a default WSL2 distribution with Python 3"
        ),
    )
    try:
        guest_identity = json.loads(identity_result.stdout)
        distribution = guest_identity["distribution"]
        linux_uid = guest_identity["uid"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise WslMiningError("Windows WSL default distribution identity is invalid") from exc
    if (
        not isinstance(distribution, str)
        or _WSL_DISTRIBUTION.fullmatch(distribution) is None
        or isinstance(linux_uid, bool)
        or not isinstance(linux_uid, int)
        or linux_uid < 0
        or linux_uid > 2**31 - 1
    ):
        raise WslMiningError("Windows WSL default distribution identity is invalid")
    try:
        confirmed_host_identity = identity_factory()
    except WindowsActiveUserError as exc:
        raise WslMiningError(str(exc)) from exc
    if confirmed_host_identity != host_identity:
        raise WslMiningError("active Windows user/session changed during WSL preflight")
    identity = WslLaunchIdentity(
        windows_session_id=host_identity.session_id,
        windows_user_sid=host_identity.user_sid,
        distribution=distribution,
        linux_uid=linux_uid,
    )
    distribution_prefix = [str(wsl), "--distribution", distribution]

    # These fixed probes also distinguish an installed-but-unconfigured WSL
    # command from a usable WSL2 GPU runtime.  No automatic installation or
    # distribution mutation is performed.
    for command, failure in (
        (
            [*distribution_prefix, "--exec", "/usr/bin/test", "-e", "/dev/dxg"],
            "Windows WSL2 NVIDIA GPU mapping (/dev/dxg) is unavailable",
        ),
        (
            [
                *distribution_prefix,
                "--exec",
                "/usr/bin/test",
                "-x",
                "/usr/lib/wsl/lib/nvidia-smi",
            ],
            "Windows WSL2 NVIDIA runtime is unavailable",
        ),
        (
            [*distribution_prefix, "--exec", "/usr/lib/wsl/lib/nvidia-smi", "-L"],
            "Windows WSL did not report an NVIDIA GPU",
        ),
        (
            [*distribution_prefix, "--exec", "/usr/bin/test", "-x", "/usr/bin/python3"],
            "Windows WSL Python runtime is unavailable",
        ),
    ):
        _run_checked(runner, command, timeout=15, failure=failure)

    temporary = WslMiningRuntime(
        wsl_executable=wsl,
        target_executable=target,
        linux_executable="/invalid",
        linux_cwd="/invalid",
        identity=identity,
        cache_key=cache_spec.key,
        runner=runner,
        popen_factory=popen_factory,
    )
    source_root = temporary.translate_path(verified_package_root)
    linux_executable, linux_cwd = _prepare_guest_package_cache(
        wsl,
        identity,
        source_root,
        cache_spec,
        runner=runner,
    )
    temporary.linux_executable = linux_executable
    temporary.linux_cwd = linux_cwd
    _assert_active_identity(identity, identity_factory)
    # Execute permission is checked only on the content-bound ext4 cache.  The
    # protected Windows source remains RX and never needs a DrvFS executable
    # bit or chmod mutation.
    _run_checked(
        runner,
        [*distribution_prefix, "--exec", "/usr/bin/test", "-x", linux_executable],
        timeout=10,
        failure="WSL cached miner package does not contain an executable Linux binary",
    )
    _assert_active_identity(identity, identity_factory)
    return temporary


__all__ = [
    "WSL_GPU_MINING_CAPABILITY",
    "WslMiningError",
    "WslMiningRuntime",
    "WslLaunchIdentity",
    "ensure_wsl_package_read_access",
    "prepare_wsl_gpu_runtime",
    "new_wsl_supervisor_token",
    "query_wsl_guest",
    "stop_wsl_guest",
    "wait_wsl_guest_ready",
    "wsl_cached_executable_matches",
    "wsl_executable_path",
]
