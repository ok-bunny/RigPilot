# RigPilot Windows CPU temperature helper

This small, read-only helper enables only LibreHardwareMonitor's CPU hardware
category and emits one bounded JSON object containing a positively identified
CPU package/die temperature. RigPilot invokes it with the fixed `--json`
argument and enforces a process timeout.

The project pins `LibreHardwareMonitorLib` 0.9.6 from NuGet and targets the
.NET Framework 4.7.2 runtime included with supported Windows 10/11 systems.
LibreHardwareMonitor is licensed under MPL-2.0 and may require administrator or
SYSTEM rights for some CPU sensors. The portable Agent includes the applicable
license and source links in its third-party notices.
