RigPilot offline PCI board identity data
======================================

The maintained PCI ID Project database is mirrored in pciutils/pciids:
https://github.com/pciutils/pciids
https://pci-ids.ucw.cz/

pci.ids and SOURCE.json are pinned local source inputs. gpu-identities.json is
the canonical generated registry. work/update_pci_identity_data.py generates
that JSON plus server/pci_identity.py and server/static/pci-identity-data.js.
All consumers operate offline. No heartbeat or UI request downloads PCI data.

Selection retains all top-level vendor IDs, original names and every NVIDIA
device whose registered name contains GeForce RTX 40xx/50xx, including Ti,
SUPER, Laptop and Mobile variants, with all registered child subsystems.
The initial fixed 2026.10.09 snapshot contains 2519 vendors, 49 selected
devices, and no registered subsystems under those selected devices.
An empty subsystem table means unregistered; no board records are invented.

brand_aliases preserves RigPilot's existing 12 display labels and adds
Gainward / CardExpert for 10b0. The original registered name remains
CardExpert Technology. These are display aliases, not DRAM-maker evidence.
PCI vendor, device and subsystem names cannot establish installed DRAM chips.

Regenerate or verify from existing inputs (no network):
  python3 work/update_pci_identity_data.py
  python3 work/update_pci_identity_data.py --check

Manual source update:
1. Review a full commit ID in the official pciutils/pciids repository.
2. Download only that fixed commit's pci.ids and README.md from
   https://raw.githubusercontent.com/pciutils/pciids/<full-commit>/<filename>
   using normal project/tool authorization. Retain the readme as source
   evidence with the update record; do not fetch a floating master in runtime.
3. Update SOURCE.json with that commit, its date/URL, and actual SHA-256 and
   byte size for each downloaded source file. Review upstream permission.
4. Replace local pci.ids, retain the copyright/license, regenerate the three
   outputs, review the data diff, and run tests/test_pci_identity.py.
5. Include LICENSE in distributed source/binary documentation. Both generated
   modules also retain the complete data-license notice in comments and a
   runtime constant, so a frozen Python executable retains the notice.

The upstream database offers GPL-2.0-or-later or BSD-3-Clause. RigPilot chooses
BSD-3-Clause for this derived data. See LICENSE and SOURCE.json.
