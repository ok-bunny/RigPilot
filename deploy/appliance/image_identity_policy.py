"""Factory identity paths and shell rendering; callers retain every gate stage."""
from __future__ import annotations

import shlex


AGENT_PATHS = (
    "var/lib/rigpilot-agent/state.json",
    "var/lib/rigpilot-agent/.state.json.replacement-pending",
    "var/lib/rigpilot-agent/.state.json.enrollment-pending",
    "var/lib/rigpilot-agent/firstboot-registration.json",
    "etc/rigpilot-agent/config.json",
    "etc/netplan/20-rigpilot-wifi.yaml",
    "run/rigpilot-provision/rigpilot-wifi.json",
    "run/rigpilot-provision/.rigpilot-enrollment-retry",
    "run/rigpilot-provision/rigpilot-enroll.json",
    "run/rigpilot-provision/rigpilot-replace.json",
)
PROVISION_FILES = ("rigpilot-wifi.json", ".rigpilot-enrollment-retry",
                   "rigpilot-enroll.json", "rigpilot-replace.json")
FACTORY_PATHS = ("var/lib/dbus/machine-id", "var/lib/systemd/random-seed",
                 "var/lib/systemd/timesync/clock", "var/lib/rigpilotos/root-grown")
SIGNING_PATHS = ("var/lib/dkms/mok.key", "var/lib/dkms/mok.pub",
                 "var/lib/shim-signed/mok/MOK.priv", "var/lib/shim-signed/mok/MOK.der")
TEMPORARIES = (
    ("var/lib/rigpilot-agent", ".state.json.*.tmp"),
    ("etc/rigpilot-agent", ".config.json.appliance-*"),
    ("var/lib/rigpilot-agent", ".firstboot-registration.json.appliance-*"),
)


def joined(root, relative):
    return root.rstrip("/") + "/" + relative


def forbidden_paths(root="/", provision=None, *, factory=False, signing=False):
    paths = AGENT_PATHS + (FACTORY_PATHS if factory else ()) + (SIGNING_PATHS if signing else ())
    result = tuple(joined(root, name) for name in paths)
    if provision is not None:
        result += tuple(joined(provision, name) for name in PROVISION_FILES)
    return result


def quote_path(path):
    # Only the fixed mount-variable forms used by the offline callers expand.
    if path.startswith(("$root/", "$provision/")):
        return '"' + path + '"'
    return shlex.quote(path)


def unregistered_guard(root="/", provision=None, *, factory=False, signing=False,
                       parent_checks=False, sudo_files=False, sudo_find=False):
    command = "sudo " if sudo_files else ""
    paths = " \\\n  ".join(quote_path(path) for path in forbidden_paths(root, provision, factory=factory, signing=signing))
    lines = [f"for forbidden_path in \\\n  {paths}; do", f'{command}test ! -e "$forbidden_path"',
             f'{command}test ! -L "$forbidden_path"', "done"]
    if parent_checks:
        for directory in ("var/lib/rigpilot-agent", "etc/rigpilot-agent"):
            path = quote_path(joined(root, directory))
            lines += [f"test ! -L {path}", f"test ! -e {path} || test -d {path}"]
    for directory, pattern in TEMPORARIES:
        path = quote_path(joined(root, directory))
        find = ("sudo " if sudo_find else "") + f"find {path} -maxdepth 1 -name {shlex.quote(pattern)} -print -quit"
        # Keep find's exit status: embedding it in `test -z` would accept a
        # failed scan as an empty directory. Missing directories are allowed.
        lines += [f"if {command}test -d {path}; then",
                  f'identity_temporary="$({find})"',
                  'test -z "$identity_temporary"', "fi"]
    return "\n".join(lines) + "\n"
