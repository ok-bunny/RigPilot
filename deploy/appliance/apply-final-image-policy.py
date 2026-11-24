#!/usr/bin/env python3
"""Apply reviewed factory memory defaults and retain the exact installed DKMS deb.

Operates on an offline mounted RigPilotOS target only. Does not change sysctl,
clear huge pages, install packages, start services, or run archive contents.
"""
from pathlib import Path
from runpy import run_path

IMAGE_RELEASE = run_path(str(Path(__file__).with_name("appliance_metadata.py")))["IMAGE_RELEASE"]
NVIDIA_DRIVER_VERSION = run_path(str(Path(__file__).with_name("appliance_metadata.py")))["NVIDIA_DRIVER_VERSION"]
import argparse,hashlib,io,json,os,re,stat,subprocess,tarfile
DEB_NAME='dkms_3.4.3-1ubuntu1_all.deb'
DEB_SHA='6cb516fc913f1afbe8387078b357e15619e19291b9398e0b3cb6d0beae99e2b4'
DEB_SIZE=57038
VERSION='1:3.4.3-1ubuntu1'
WIRELESS_REGDB_VERSION='2026.05.30-0ubuntu1~24.04.1'
WIRELESS_REGDB_PACKAGE_SHA='e4ee541d99f009f1225100fead946907afe3ad9b614fd5707f7363d7a9c03d39'
WIRELESS_REGDB_INRELEASE_SHA='9bc40e92bbd52dd05b1c35eea3d6788b39341cfcf9d75c7c4c405d13dcd8b9c1'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def safe(root,relative):
    parts=Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(x in ['.','..'] for x in parts):raise RuntimeError('Unsafe relative path')
    p=root
    for part in parts:
        p=p/part
        if p.is_symlink():raise RuntimeError('Symlink at offline policy boundary: '+relative)
    return p
def atomic(path,data,mode):
    temp=path.with_name('.'+path.name+'.rigpilotos-policy-tmp')
    if temp.exists() or temp.is_symlink():raise RuntimeError('Unfinished policy transaction')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    try:
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        os.chown(temp,0,0);os.chmod(temp,mode);os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()
def installed_dkms_executables(root,data):
    # The caller pins the archive SHA; verify every executable it contains.
    executables=[]
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:') as archive:
        for member in archive:
            if member.issym() or member.islnk():raise RuntimeError('Unexpected link in fixed DKMS data archive')
            if not member.isfile() or not member.mode&0o111:continue
            name=member.name.removeprefix('./');path=safe(root,name);st=path.lstat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_gid!=0 or stat.S_IMODE(st.st_mode)!=member.mode:raise RuntimeError('Untrusted DKMS executable: '+name)
            expected_sha=hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            if digest(path)!=expected_sha:raise RuntimeError('Installed DKMS executable differs: '+name)
            executables.append({'path':name,'sha256':expected_sha})
    if 'usr/sbin/dkms' not in {x['path'] for x in executables}:raise RuntimeError('Unexpected DKMS executable set')
    return executables
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--provision-root',type=Path,required=True,help='mounted registration (vfat) partition of the same image');parser.add_argument('--dkms-deb',type=Path,required=True);parser.add_argument('--agent-version',required=True);parser.add_argument('--build-revision',required=True);args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Offline target policy requires root')
    root=args.root.resolve(strict=True)
    if root==Path('/') or not root.is_dir():raise RuntimeError('Refusing host root')
    provision=args.provision_root.resolve(strict=True)
    if provision==Path('/') or not provision.is_dir():raise RuntimeError('Refusing host provision root')
    if provision==root:raise RuntimeError('Provision root must be the separate registration partition')
    if not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',args.agent_version):raise RuntimeError('Invalid Agent version')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',args.build_revision):raise RuntimeError('Invalid revision')
    if tuple(map(int,args.agent_version.split('.'))) < (0,8,22):raise RuntimeError('Wired-only image requires Agent 0.8.22 or newer')
    source=subprocess.check_output(['findmnt','-n','-o','SOURCE','--target',str(root)],text=True).strip()
    filesystem=subprocess.check_output(['findmnt','-n','-o','FSTYPE','--target',str(root)],text=True).strip()
    if filesystem!='ext4' or not re.fullmatch(r'/dev/(nbd[0-9]+p4|vdb4)',source):raise RuntimeError('Expected isolated image root partition')
    if subprocess.check_output(['findmnt','-n','-o','MAJ:MIN','--target',str(root)],text=True)==subprocess.check_output(['findmnt','-n','-o','MAJ:MIN','/'],text=True):raise RuntimeError('Target is host filesystem')
    release=safe(root,'etc/rigpilotos-release');values=dict(line.split('=',1) for line in release.read_text().splitlines() if '=' in line)
    expected={'NAME':'RigPilotOS','VERSION':IMAGE_RELEASE,'UBUNTU_VERSION':'24.04','IMAGE_KERNEL':'6.8.0-142-generic','NVIDIA_DRIVER_VERSION':NVIDIA_DRIVER_VERSION,'AGENT_SEED_VERSION':args.agent_version,'WIFI_SUPPORT':'disabled'}
    for key,value in expected.items():
        if values.get(key)!=value:raise RuntimeError('Unexpected target '+key)
    if 'WPA_DEFAULT_START' in values:raise RuntimeError('Legacy WPA release policy is not allowed')
    if digest(safe(root,'boot/config-6.8.0-142-generic'))!='3b8533dd9d235ca634ac58f82c5ce1ee35f12ef620693e17033184d2c9ca5890':raise RuntimeError('Exact GA142 kernel config mismatch')
    for name in ['var/lib/rigpilot-agent/state.json','var/lib/rigpilot-agent/firstboot-registration.json','var/lib/rigpilot-agent/.state.json.enrollment-pending','var/lib/rigpilot-agent/.state.json.replacement-pending','var/lib/rigpilot-agent/pending-hiveos-start.json','var/lib/rigpilot-agent/hiveos-components','etc/rigpilot-agent/config.json','etc/netplan/20-rigpilot-wifi.yaml','run/rigpilot-provision/rigpilot-wifi.json','opt/rigpilot-agent/Setup-HiveOS-Runtime.sh','etc/systemd/system/rigpilot-agent.service.d/remote-sessions.conf']:
        if safe(root,name).exists():raise RuntimeError('Target is not a factory image: '+name)
    # The registration partition is a separate noexec vfat mount; the root-tree
    # bwrap scan below cannot cross into it, so validate and scan it directly.
    provision_source=subprocess.check_output(['findmnt','-n','-o','SOURCE','--target',str(provision)],text=True).strip()
    provision_filesystem=subprocess.check_output(['findmnt','-n','-o','FSTYPE','--target',str(provision)],text=True).strip()
    if provision_filesystem!='vfat' or not re.fullmatch(r'/dev/(nbd[0-9]+p1|vdb1)',provision_source):raise RuntimeError('Expected isolated image registration partition')
    if subprocess.check_output(['findmnt','-n','-o','MAJ:MIN','--target',str(provision)],text=True)==subprocess.check_output(['findmnt','-n','-o','MAJ:MIN','/'],text=True):raise RuntimeError('Provision target is host filesystem')
    # 0.2.11 removal batch: the retired HiveOS account and the private
    # bubblewrap runtime must not exist anywhere in the factory tree.
    for passwd_line in safe(root,'etc/passwd').read_text().splitlines():
        if passwd_line.split(':',1)[0]=='rigpilot-hiveos':raise RuntimeError('Retired rigpilot-hiveos account remains in factory image')
    for group_line in safe(root,'etc/group').read_text().splitlines():
        if group_line.split(':',1)[0]=='rigpilot-hiveos':raise RuntimeError('Retired rigpilot-hiveos group remains in factory image')
    import subprocess as _sp
    bwrap=_sp.run(['find',str(root),'-xdev','-name','bwrap','-o','-name','bwrap-*','-o','-name','hiveos-runtime'],capture_output=True,text=True)
    if bwrap.returncode!=0 or bwrap.stdout.strip():raise RuntimeError('Private bubblewrap/HiveOS runtime remnant remains in factory image')
    provision_bwrap=_sp.run(['find',str(provision),'-name','bwrap','-o','-name','bwrap-*','-o','-name','hiveos-runtime'],capture_output=True,text=True)
    if provision_bwrap.returncode!=0 or provision_bwrap.stdout.strip():raise RuntimeError('Private bubblewrap/HiveOS runtime remnant remains in the registration partition')
    if args.dkms_deb.is_symlink() or not args.dkms_deb.is_file() or args.dkms_deb.stat().st_size!=DEB_SIZE or digest(args.dkms_deb)!=DEB_SHA:raise RuntimeError('DKMS archive mismatch')
    fields=subprocess.check_output(['dpkg-deb','-f',str(args.dkms_deb),'Package','Version','Architecture'],text=True)
    if fields!='Package: dkms\nVersion: '+VERSION+'\nArchitecture: all\n':raise RuntimeError('DKMS archive identity mismatch')
    matches=[];wireless_regdb=[];forbidden_wifi_packages={'wpasupplicant','iw','rfkill'};retired_packages={'ubuntu-drivers-common','bubblewrap'}
    for paragraph in safe(root,'var/lib/dpkg/status').read_text().split('\n\n'):
        d=dict(line.split(': ',1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
        if d.get('Package')=='dkms':matches.append(d)
        if d.get('Package')=='wireless-regdb':wireless_regdb.append(d)
        if d.get('Package') in forbidden_wifi_packages and d.get('Status')=='install ok installed':raise RuntimeError('Wi-Fi package remains installed: '+d['Package'])
        if d.get('Package') in retired_packages and d.get('Status')=='install ok installed':raise RuntimeError('Retired HiveOS/driver-update package remains installed: '+d['Package'])
    if len(matches)!=1 or any(matches[0].get(k)!=v for k,v in {'Status':'install ok installed','Version':VERSION,'Architecture':'all'}.items()):raise RuntimeError('Installed DKMS identity mismatch')
    if len(wireless_regdb)!=1 or any(wireless_regdb[0].get(k)!=v for k,v in {'Status':'install ok installed','Version':WIRELESS_REGDB_VERSION,'Architecture':'all'}.items()):raise RuntimeError('GA modules-extra wireless-regdb signed dependency differs')
    # 0.2.12+: the driver is run-managed.  No CUDA repository may be
    # configured, and the apt preference guard must exist so distribution
    # NVIDIA packages can never shadow the run-installed files.
    if safe(root,'etc/apt/sources.list.d/cuda-ubuntu2404-x86_64.list').exists():raise RuntimeError('CUDA repository source remains in run-managed image')
    if safe(root,'usr/share/keyrings/cuda-archive-keyring.gpg').exists():raise RuntimeError('CUDA repository keyring remains in run-managed image')
    prefs=safe(root,'etc/apt/preferences.d/99-rigpilot-nvidia-run-managed')
    if not prefs.is_file():raise RuntimeError('NVIDIA apt preference guard is missing')
    prefs_text=prefs.read_text()
    if 'Pin-Priority: -1' not in prefs_text or 'nvidia-*' not in prefs_text:raise RuntimeError('NVIDIA apt preference guard is incomplete')
    data=subprocess.check_output(['dpkg-deb','--fsys-tarfile',str(args.dkms_deb)])
    executables=installed_dkms_executables(root,data)
    parent=safe(root,'var/cache/apt/archives');st=parent.stat()
    if st.st_uid!=0 or st.st_gid!=0 or stat.S_IMODE(st.st_mode)&0o022:raise RuntimeError('APT archive directory is not root protected')
    destination=safe(root,'var/cache/apt/archives/'+DEB_NAME);atomic(destination,args.dkms_deb.read_bytes(),0o600)
    if digest(destination)!=DEB_SHA:raise RuntimeError('Retained DKMS verification failed')
    for name in ['etc/systemd/system/rigpilotos-wifi-bootstrap.service','etc/systemd/system/rigpilotos-wifi-bootstrap.path','etc/systemd/system/rigpilotos-wifi-bootstrap.timer','usr/libexec/rigpilotos-wifi-bootstrap','etc/systemd/system/multi-user.target.wants/wpa_supplicant.service','etc/systemd/system/dbus-fi.w1.wpa_supplicant1.service','usr/share/dbus-1/system-services/fi.w1.wpa_supplicant1.service','usr/lib/systemd/system/wpa_supplicant.service','usr/lib/systemd/system/wpa_supplicant@.service','usr/lib/systemd/system/wpa_supplicant-nl80211@.service','usr/lib/systemd/system/wpa_supplicant-wired@.service']:
        path=safe(root,name)
        if path.exists() or path.is_symlink():raise RuntimeError('Wi-Fi runtime remains in factory image: '+name)
    values.update(BUILD_REVISION=args.build_revision,HUGETLB_DEFAULT_RESERVATION='none',DKMS_RETAINED_VERSION=VERSION,DKMS_RETAINED_SHA256=DEB_SHA)
    atomic(release,(''.join(k+'='+v+'\n' for k,v in values.items())).encode(),0o644)
    directory=safe(root,'usr/share/rigpilotos');directory.mkdir(exist_ok=True)
    policy=('RigPilotOS GPU appliance memory policy\n\nNo default hugetlb pool is provisioned. Factory boot acceptance requires HugePages_Total=0 and Hugetlb=0.\nNo runtime sysctl reset, cache dropper, swap change, or huge-page cleanup daemon is installed by this policy.\nFuture CPU workloads may request huge pages explicitly with workload ownership and release semantics.\nWired Ethernet DHCP is the only appliance network enrollment path. Wi-Fi credentials, bootstrap units and WPA runtime packages are absent. Ubuntu GA modules-extra requires the passive wireless-regdb data package; it does not configure an interface.\nNetwork, resolver, time synchronization, SSH, logging, Agent and required driver services are retained.\n')
    atomic(directory/'MEMORY-POLICY.txt',policy.encode(),0o644)
    report={'agent_version':args.agent_version,'build_revision':args.build_revision,'retained_dkms':{'path':'/var/cache/apt/archives/'+DEB_NAME,'version':VERSION,'size':DEB_SIZE,'sha256':DEB_SHA,'uid':0,'gid':0,'mode':'0600','installed_executables':executables},'nvidia_management':{'mode':'run-installer','driver_version':NVIDIA_DRIVER_VERSION,'kernel_module':'open','cuda_repository_absent':True,'apt_preference_guard':'/etc/apt/preferences.d/99-rigpilot-nvidia-run-managed'},'wifi_support':'disabled','wifi_runtime_packages_absent':sorted(forbidden_wifi_packages),'retired_feature_packages_absent':sorted(retired_packages),'retired_feature_runtime':{'hiveos_account_absent':True,'hiveos_components_absent':True,'private_bubblewrap_absent':True,'remote_sessions_dropin_absent':True},'wireless_regdb_kernel_dependency':{'required_by':'linux-modules-extra-6.8.0-142-generic','version':WIRELESS_REGDB_VERSION,'package_sha256':WIRELESS_REGDB_PACKAGE_SHA,'inrelease_sha256':WIRELESS_REGDB_INRELEASE_SHA},'wired_dhcp_required':True,'hugetlb_runtime_value_modified':False}
    atomic(directory/'factory-policy.json',(json.dumps(report,indent=2)+'\n').encode(),0o644);os.sync();print(json.dumps(report))
if __name__=='__main__':main()
