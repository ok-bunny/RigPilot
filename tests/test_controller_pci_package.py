"""Preserve actual generated PCI consumers in an isolated offline payload."""

import hashlib
import json
from pathlib import Path
import tarfile

from test_release_gates_review import fake_runtime, load
from test_release_scripts import assert_controller_allowlists_match


ROOT = Path(__file__).resolve().parents[1]
PCI_CONSUMERS = ("server/pci_identity.py", "server/static/pci-identity-data.js")


def test_generated_pci_consumers_are_in_the_offline_controller_payload(tmp_path):
    builder = load("build-controller-release.py")
    assert_controller_allowlists_match(builder, ROOT)
    assert set(PCI_CONSUMERS).issubset(builder.ALLOWED_FILES)
    actual_payloads = {name: (ROOT / name).read_bytes() for name in PCI_CONSUMERS}

    # Reuse the existing synthetic wheel/runtime fixture. This builds only a
    # tiny temporary package, with the actual generated consumer bytes; it is
    # neither a formal Controller release nor a native runtime qualification.
    source = tmp_path / "synthetic source"
    fake_runtime(source, builder, 1)
    (source / "server/__init__.py").write_text('__version__ = "1.2.3"\n')
    for name, payload in actual_payloads.items():
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    builder.ALLOWED_FILES += (
        "server/__init__.py",
        "server/requirements.txt",
        builder.RUNTIME_ROOT + "/runtime-target.json",
        builder.RUNTIME_ROOT + "/requirements-controller-linux-py310.lock",
        *PCI_CONSUMERS,
    )
    archive_path, _, _ = builder.build("1.2.3", tmp_path / "synthetic output")
    load("verify-release-artifacts.py").verify_controller(archive_path, "1.2.3")
    with tarfile.open(archive_path, "r:gz") as archive:
        manifest = json.load(archive.extractfile("controller-release.json"))
        for name, payload in actual_payloads.items():
            assert archive.extractfile(name).read() == payload
            assert manifest["files"][name] == hashlib.sha256(payload).hexdigest()
