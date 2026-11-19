# ASUS laptops' fan speeds through the ATK ACPI device

Research date: 2026-11-17. Question: can lag-doctor record an ASUS laptop's CPU, GPU and middle fan speeds, strictly
read-only and without administrator rights, through the ASUS System Control Interface driver's device
`\\.\ATKACPI`, and say "unavailable" with a reason on every other PC? The point is to tell, when heat holds a GPU
down, whether the fans were already running fast. Built on 2026-11-17: `lagdoctor/winapi/atkacpi.py` reads, and
`lagdoctor/capture/fanmon.py` records the fans once a second (DESIGN.md, Collector specifics, fans). Section 9 has
what that reader measured on an ASUS laptop.

Method: two independent implementations of the interface were read. G-Helper (C#, GPL-3.0) at tag v0.291 (commit
`f62d584`, released 2026-11-02) and on main at `aa0037c` (2026-11-17), where every fan line read here is unchanged.
The Linux kernel's asus-wmi driver at master `63fdb51` (2026-11-17), which reaches the same firmware methods through
ACPI WMI instead of the Windows device. A third Windows tool, atrofac (Rust, created 2020, before G-Helper), was read
for the transport only: it sends sets, never reads. One decompiled ASUS firmware table (DSDT excerpts of an ASUS
Zenbook, BIOS 303, published in a third-party repository) shows what the firmware does with each call, and a 2020
reverse-engineering note on a G14 names the same ids. Microsoft's documentation covers CreateFileW, DeviceIoControl,
I/O control codes and path normalization. The research itself opened nothing: on the laptop it lists only what
Windows shows without opening the device. Section 9 is the one run that opened it, with lag-doctor's own read-only
code, after the research.

**Tags.**
- `[verified: …]`: read at that source in this research (file and line, or the page named).
- `[measured: …]`: read on an ASUS laptop by the command named (section 2 without opening the device, section 9
  through it).
- `[forum: …]`: users' or contributors' posts, not code or vendor documentation.
- `[inferred]`: reasoned, not observed.
- `[unverified]`: could not be checked.

## 1. The short answer

- One call per fan: open `\\.\ATKACPI`, send control code `0x0022240C` with a 16-byte input that names the method
  DSTS ("device status") and the fan's device id, and read a 32-bit little-endian answer. Bit 16 (`0x00010000`) says
  the fan is present; the low 16 bits are its speed in hundreds of RPM. G-Helper and Linux agree on the method id,
  the three fan ids, the presence bit and the x100 (sections 3 and 4).
- Read-only cannot come from the handle. The same control code carries DSTS and DEVS ("device set"), and with DEVS
  the same three fan ids switch the fans to full speed (1) or back to automatic (0). The control code's access field
  is FILE_ANY_ACCESS, so Windows checks no read or write right per call. Read-only has to be built into lag-doctor's
  code and held by a test (sections 5, 6 and 8).
- No ASUS document describes DSTS. The evidence that it only reads is the names in Linux, how both tools use it
  (G-Helper polls it about once a second and its author swept it over every id), and one decompiled firmware where
  DSTS's fan branches only read the embedded controller while DEVS's fan branches write it (section 5). That is
  strong for the fan ids, not a guarantee for every model.
- Rights: a handle opened with no access rights at all, from an unelevated process, gets both fans answered
  [measured: section 9]. G-Helper runs as invoker and quits if it cannot open the device [verified: section 2].
  The device's own access list was not read.
- Without the driver there is no `ATKACPI` device name, CreateFileW fails, and the reading is "unavailable" with the
  error. Older ASUS firmware without these ids answers "unsupported" or "absent", which must also be unavailable,
  never 0 (section 4).

## 2. The device and the handle

- **Path.** G-Helper's constant is the C# verbatim string `@"\\.\\ATKACPI"` [verified: G-Helper `app/AsusACPI.cs`:32],
  which holds a doubled separator after the dot; Windows collapses "a series of slashes that follow the first two
  slashes" into one when it normalizes a `\\.\` path, so it opens `\\.\ATKACPI` [verified: Microsoft, "File path
  formats on Windows systems", Canonicalize separators; normalization is skipped only for paths that start exactly
  with `\\?\`]. atrofac's Rust literal is `"\\\\.\\ATKACPI"`, that is `\\.\ATKACPI` [verified: atrofac
  `library/src/atkacpi.rs`:6]. In Python, write it `r"\\.\ATKACPI"` [inferred].
- **Measured on the ASUS laptop.** `QueryDosDeviceW("ATKACPI")` returns `\Device\ATKACPI` (it reads the name's link without
  opening the device); a made-up name returns error 2 [measured: ctypes, unelevated]. Windows lists the device
  "ASUS System Control Interface v3" at `ACPI\ASUS2018\...` [measured: `Win32_PnPEntity`], and the same driver
  package (`asussci2.inf`) runs a kernel driver service `ATKWMIACPIIO` (`AsusWmiAcpi.sys`, start type System)
  [measured: `Win32_SystemDriver`]. That this service owns `\Device\ATKACPI` is [inferred] from the package and the
  name.
- **Which driver.** G-Helper's message when the open fails is "Can't connect to ASUS ACPI. Application can't function
  without it. Try to install Asus System Control Interface" [verified: G-Helper `app/Properties/Strings.resx`:123-124],
  and its README says it works "by using the Asus System Control Interface 'driver' that Armoury uses" [verified:
  G-Helper `docs/README.md`:260]. ASUS's FAQ names the driver in Device Manager as "ASUS System Control Interface",
  in two generations (V3 and V1 installers), installed from the model's support page [verified: ASUS FAQ 1047338,
  updated 2025-05-23; the FAQ is about a Windows 11 upgrade fix and does not describe the interface].
- **CreateFileW arguments.** G-Helper: `GENERIC_READ | GENERIC_WRITE`, `FILE_SHARE_READ | FILE_SHARE_WRITE`, no
  security attributes, `OPEN_EXISTING`, `FILE_ATTRIBUTE_NORMAL`, no template [verified: `app/AsusACPI.cs`:298-306, the
  constants at 245-250]. atrofac: `0xC0000000` (the same two rights), share `3`, `OPEN_EXISTING`, flags `0`
  [verified: atrofac `library/src/device_control.rs`:23-31]. Microsoft: "You should specify the FILE_SHARE_READ and
  FILE_SHARE_WRITE access flags when calling CreateFile to open a handle to a device driver", with `OPEN_EXISTING`
  and no template [verified: DeviceIoControl, Remarks]. G-Helper keeps one handle for the app's whole life and
  synchronous I/O (no `FILE_FLAG_OVERLAPPED`, `lpOverlapped` null) [verified: `app/AsusACPI.cs`:294-308, 388-401].
- **Failure value.** CreateFileW returns `INVALID_HANDLE_VALUE` on failure [verified: CreateFileW, Return value].
  G-Helper tests `handle != -1` [verified: `app/AsusACPI.cs`:308]; atrofac tests `!= null_mut()`, which takes a failed
  open for an open handle [verified: `device_control.rs`:33]. A missing device name should fail with
  ERROR_FILE_NOT_FOUND (2) [inferred from the QueryDosDeviceW reading above; CreateFileW itself was not run].
- **Rights.** G-Helper's manifest requests `asInvoker` [verified: `app/app.manifest`:7], it quits on an ASUS laptop
  when the device does not open [verified: `app/Program.cs`:99-111], and its title marks an elevated run with a
  different dash, so both runs are normal [verified: `app/Settings.cs`:288]. So the open with read and write rights
  works from an unelevated token in G-Helper's ordinary use [inferred]. A contributor's open pull request (G-Helper's
  pull request 6202) for standard (non-administrator) accounts lists what needs administrator rights: Ryzen power
  limits, NVIDIA clocks and stopping ASUS's services; the ACPI calls are not among them [forum: that pull request's
  text]. Not measured on this PC; the device's security descriptor was not read (that needs a handle).

## 3. The call: control code and buffers

- **Control code.** `0x0022240C` [verified: G-Helper `app/AsusACPI.cs`:33; atrofac `atkacpi.rs`:7 writes it as
  2237452, the same number]. Decoded with `CTL_CODE`'s layout (device type bits 16-30, access 14-15, function 2-12,
  method 0-1) [verified: Microsoft, "Defining I/O Control Codes"]: device type `0x22` = FILE_DEVICE_UNKNOWN, access
  0 = FILE_ANY_ACCESS, function `0x903` (custom bit 13 set), method 0 = METHOD_BUFFERED [verified: SDK 10.0.26100
  `um\winioctl.h`:202, 287, 316, and the arithmetic]. With FILE_ANY_ACCESS, "the I/O manager sends the IRP for any
  caller that has a handle"; a driver may check more strictly itself (IoValidateDeviceIoControlAccess) [verified:
  same page]. Whether this driver does is [unverified].
- **Input buffer.** An 8-byte header, then the method's arguments, all little-endian 32-bit [verified: G-Helper
  `CallMethod`, `app/AsusACPI.cs`:410-424]:

  | bytes | field | DSTS for a fan |
  |---|---|---|
  | 0-3 | method id | `0x53545344`, the bytes `44 53 54 53`, ASCII "DSTS" |
  | 4-7 | length of the arguments | `8` |
  | 8-11 | device id | `0x00110013` CPU, `0x00110014` GPU, `0x00110031` middle |
  | 12-15 | second argument | `0` |

  G-Helper's `DeviceGet` fills exactly this, 16 bytes in all [verified: `app/AsusACPI.cs`:490-497]. atrofac's DEVS
  templates use the same header byte for byte: `44 45 56 53` ("DEVS"), `08 00 00 00`, then `75 00 12 00` for device
  `0x00120075` [verified: `atkacpi.rs`:8-9], which shows the little-endian layout on the wire independently of C#'s
  BitConverter. The CPU fan's call is therefore `44 53 54 53 08 00 00 00 13 00 11 00 00 00 00 00` [inferred from the
  two].
- **Output buffer.** G-Helper passes 16 bytes and reads the first 4 as a little-endian Int32 [verified:
  `app/AsusACPI.cs`:413, 496]; its id scan passes 64 [verified: `DeviceGetLarge`, 918-928]; atrofac passes 1024
  [verified: `atkacpi.rs`:43]. With METHOD_BUFFERED the system allocates one buffer the size of the larger of the
  two lengths [verified: Microsoft, "Buffer Descriptions for I/O Control Codes"]. How many bytes this driver reports
  in `lpBytesReturned` is [unverified]: neither tool reads it.
- **Errors.** DeviceIoControl returns zero on failure (GetLastError gives the reason), and with `lpOverlapped` null,
  `lpBytesReturned` must not be null [verified: DeviceIoControl, Return value and Parameters]. G-Helper ignores the
  return value [verified: `app/AsusACPI.cs`:388-401], so a failed call leaves its zeroed buffer, which it then reads
  as an absent fan (section 4).
- **The same call in Linux.** asus-wmi evaluates the WMI method with the method id and a packed argument block whose
  first two 32-bit fields are the device id and 0 [verified: `drivers/platform/x86/asus-wmi.c`:181-188, 370-411,
  564-575]. The firmware sees the same two numbers: the decompiled ASUS `WMNB` method reads its argument buffer's
  first and second dwords as `IIA0` and `IIA1` and dispatches on the method id [verified: DSDT excerpts, lines
  714-718, the DSTS branch from 828, DEVS from 1238].

## 4. Fan ids, units, presence and the values that mean "no reading"

- **Ids.** CPU `0x00110013`, GPU `0x00110014`, middle `0x00110031` in both sources [verified: G-Helper
  `app/AsusACPI.cs`:63-65; Linux `include/linux/platform_data/x86/asus-wmi.h`:100-102, where they are named
  `ASUS_WMI_DEVID_CPU_FAN_CTRL`, `GPU_FAN_CTRL` and `MID_FAN_CTRL`]. `0x00110012` is the deprecated `FAN_CTRL`
  [verified: asus-wmi.h:99].
- **Method ids.** DSTS `0x53545344` "Device status", DEVS `0x53564544` "DEVice Set", INIT `0x54494E49` [verified:
  asus-wmi.h:25, 27, 30; G-Helper `app/AsusACPI.cs`:35-37].
- **Units and conversion.** Both take the low 16 bits and multiply by 100 for RPM:
  - Linux: `value &= 0xffff` and prints `value * 100` for each of the three fans [verified: asus-wmi.c:3107 and 3126
    (CPU), 3239 and 3241 (GPU), 3257 and 3259 (middle)].
  - G-Helper: `DeviceGet` returns the answer minus 65536 (`0x10000`, the presence bit) [verified:
    `app/AsusACPI.cs`:496]; `GetFan` keeps `raw & 0xFFFF` [verified: 569]; the window shows `value * 100` "RPM" or a
    percentage of a per-model maximum [verified: `app/Fan/FanSensorControl.cs`:125-134], and the overlay stores
    `GetFan(...) * 100` [verified: `app/HardwareControl.cs`:906-907]. Subtracting `0x10000` leaves the low 16 bits
    unchanged, so the two agree on the speed [inferred, arithmetic].
  - The firmware: the decompiled DSTS branch for `0x00110013` reads the controller's tachometer through `RTAH (Zero)`,
    divides by 100 (`0x64`) and returns `0x00010000 + value`; for `0x00110014` it does the same with `RTAH (One)`
    [verified: DSDT excerpts, lines 1055-1074]. The division truncates, so the resolution is 100 RPM [inferred]. A
    2020 note on a G14 gives the same rule, "the RPM is (HEX - 0x00010000) * 0x64" [verified: zllovesuki
    `G14/wmi.txt`:21-26].
- **Presence and status bits.** Linux: `ASUS_WMI_DSTS_PRESENCE_BIT 0x00010000`, `STATUS_BIT 0x00000001`, `UNKNOWN_BIT
  0x00000002`, `USER_BIT 0x00020000`, `BIOS_BIT 0x00040000` [verified: asus-wmi.h:173-177]. A fan is exposed only if
  its id answers with the presence bit at start-up [verified: `asus_wmi_dev_is_present`, asus-wmi.c:654-661, used in
  `asus_wmi_fan_init`, 3453-3482]. The status and unknown bits are for on/off devices and are not read for fans
  [verified: the fan readers at 3092-3127 and 3227-3259 use only the low 16 bits].
- **"No reading" values.**

  | answer | meaning | source |
  |---|---|---|
  | `0x00010000` | present, 0 RPM (stopped) | firmware returns `0x00010000 + 0` [verified: DSDT 1059]; G-Helper reads 0, not absent [verified: `GetFan`, 570] |
  | `0x00000000` | absent | the firmware's GPU branch returns `Zero` when a controller flag says there is no GPU fan [verified: DSDT 1062-1074]; G-Helper turns 0 with the presence bit clear into -1 [verified: 570]; Linux does not expose a fan without the presence bit [verified: 654-661] |
  | `0xFFFFFFFE` | unsupported method or id | `ASUS_WMI_UNSUPPORTED_METHOD` [verified: asus-wmi.h:34], turned into -ENODEV [verified: asus-wmi.c:404-408]; the decompiled `WMNB` returns it when no branch matches [verified: DSDT 1748]; G-Helper rejects it because its low word `0xFFFE` exceeds 120 [verified: 570] |
  | `0xFFFFFFFF` | no device | Linux's `asus_wmi_get_devstate` returns -ENODEV for `~0` [verified: asus-wmi.c:573-574] |
  | an all-zero buffer | the call failed and nothing was written | G-Helper reads it as absent because it ignores the return value [verified: 388-401, 496, 570] |

  `0xFFFFFFFE` has bit 16 set, so a check of the presence bit alone would accept it as "present, 65,534 x 100 RPM"
  [inferred, arithmetic]: test the sentinels first. Linux also says "at least TUF Gaming series laptops return nothing
  for unknown methods" [verified: asus-wmi.c:5003-5004], so an unknown id may answer 0 rather than `0xFFFFFFFE`.
- **Where the two disagree.**
  - Range: G-Helper drops a low word above 120 (12,000 RPM) [verified: `GetFan`, 570]; Linux has no upper bound
    [verified: 3092-3127].
  - Presence: G-Helper accepts a non-zero low word without the presence bit (an answer of `0x00000014` reads as
    2,000 RPM) [inferred from 569-570]; Linux requires the presence bit before it shows a fan at all [verified:
    654-661, 3462-3472].
  - Method: Linux uses DCTS (`0x53544344`) instead of DSTS on machines whose ASUS WMI device has `_UID` "ASUSWMI",
    and DSTS on those with "ATK" [verified: asus-wmi.c:5006-5019; asus-wmi.h:24]. G-Helper always sends DSTS
    [verified: `app/AsusACPI.cs`:494].
  - Older firmware: Linux reads the CPU fan through `CPU_FAN_CTRL` only "starting in Spec 8.3"; before that it uses
    the AGFN method instead [verified: asus-wmi.c:231-235, 3460-3466]. G-Helper has no AGFN path [verified: no AGFN
    in `app/AsusACPI.cs`]. Such a laptop would answer DSTS for `0x00110013` with no presence bit [inferred].
- **Per-model maximum.** G-Helper's default top speed is 58 (5,800 RPM), with per-model figures between 55 and 78
  for the two main fans and up to 92 for a middle fan, and a calibration it can run [verified:
  `app/Fan/FanSensorControl.cs`:8, 50-80]. So a fan's share of its maximum needs a per-model figure that lag-doctor
  does not have; RPM itself needs none [inferred].

## 5. Is DSTS a read?

- **No ASUS document.** The interface is a vendor's private control code; Microsoft notes that private IOCTLs "aren't
  documented by Microsoft" [verified: "Defining I/O Control Codes"], and no ASUS document of DSTS or DEVS was found
  [unverified: ASUS's FAQ was the only ASUS page read].
- **Names.** Linux: DSTS "Device status (DSTS)", DEVS "DEVice Set" [verified: asus-wmi.h:25, 27]. Its set function's
  comment says a DEVS call "must be paired with" a DSTS call "to check if the WMI function is supported" [verified:
  asus-wmi.c:605-622]. G-Helper's author calls DSTS "asus'es 'get device status' command" and DEVS "set device
  status" [forum: G-Helper's issue 38, opened by its author on 2023-03-02].
- **How it is used.** Linux reads `fanN_input` with DSTS on every read of the file, under one global lock for all
  WMI calls [verified: asus-wmi.c:360-368, 3092-3127]. G-Helper reads all three fans on each sensor refresh
  [verified: `app/HardwareControl.cs`:881-898], and its "scan" sends DSTS to every id from `0x00000000` to `0x001600FF`
  [verified: `ScanRange`, `app/AsusACPI.cs`:879-914], which its author asks users to run [forum: issue 38]. Neither
  would do that with a method that changed state [inferred].
- **What one firmware does.** In the decompiled `WMNB` method, the DSTS fan branches only call the embedded
  controller's readers (`RTAH`, `RRAM`) and return a value, while the DEVS branches for the same two ids read a
  controller byte, set or clear bit `0x40` by the argument and write it back with `WRAM` [verified: DSDT excerpts,
  1055-1074 against 1510-1545]. Across that firmware's whole DSTS branch, every controller call's result is used as
  a value, and neither writer that the DEVS branch uses (`WRAM`, `STA9`) appears [verified: the same file, lines
  828-1237]. That is one model's firmware: what `RTAH` does inside is not in the excerpt [unverified], and other
  models' firmware may differ [inferred].
- **Polling in practice.** G-Helper's window refreshes sensors on a timer of `sensor_timer` ms, default 1000, while
  the window is visible (or always, if the user set `sensors_always`), and throttles each refresh to one per 2 s (6 s
  when hidden) [verified: `app/Settings.cs`:250-252, 715, 1641-1645]. Its in-game overlay reads both fans once a
  second when it shows them [verified: `app/Overlay/HardwareOverlay.cs`:194, 442; `app/HardwareControl.cs`:900-913].
  Its CPU temperature read through DSTS is cached for 2 s [verified: `app/HardwareControl.cs`:451-458]. No side
  effect of polling DSTS is documented in either source [verified: none in the code read]. One user's 7 % CPU load
  every 3 seconds went away with a build that changed the sensor timer; the author changed the battery-rate read,
  which goes through WMI, and said the timer costs "near zero" on his own laptop [forum: G-Helper's issue 5143]. So
  the cost of one DSTS call on a given laptop is unknown until measured [inferred].

## 6. Pitfalls

- **The fan ids are also setters.** With DEVS, the same ids switch the fans: Linux's `pwm1_enable` (and 2, 3) sends
  DEVS with 1 for full speed and 0 for automatic [verified: asus-wmi.c:3147-3180, 3271-3300, 3317-3345], and the 2020
  G14 note lists DEVS on `0x00110013` and `0x00110014` as "Max CPU Fan" and "Max GPU Fan", argument 0/1, which "is
  pretty scary" [verified: `G14/wmi.txt`:49-54]. A slip from DSTS to DEVS with a second argument of 1 would run the
  fans at full speed; with 0 it would hand them back to the firmware's automatic curve [inferred].
- **Linux's own driver writes at start-up.** `asus_wmi_fan_init` calls `asus_fan_set_auto`, which sends DEVS 0 on the
  CPU and GPU fan ids [verified: asus-wmi.c:3478, 2991-3031]. Copying Linux's start-up would be a write.
- **Ids near the fan ids that set things** (each is a DEVS target in G-Helper or Linux):
  - `0x00110018` fan boost mode [verified: asus-wmi.h:75];
  - `0x00110019` the Vivobook performance mode [verified: `app/AsusACPI.cs`:73];
  - `0x00110022` and `0x00110023` the CPU and GPU fan ranges [verified: 97-98, written by `SetFanRange`, 593-596];
  - `0x00110024`, `0x00110025`, `0x00110032` the fan curves, which DSTS reads only with a mode in the second argument
    [verified: 100-102, 634-661; asus-wmi.h:103-105];
  - `0x00110026`, `0x00110027`, `0x00110033` fan floors and `0x00110034` hysteresis [verified: 104-108];
  - `0x00120075` the performance mode, which also applies the mode's fan curve [verified: `app/AsusACPI.cs`:72;
    `G14/wmi.txt`:29-34].
  G-Helper's fan calibration is a write: it sets Turbo and 100 % curves before it reads the fans [verified:
  `app/Fan/FanSensorControl.cs`:137-152].
- **Other methods with effects.** INIT enables hotkeys on some models [verified: asus-wmi.c:4975-4977], and G-Helper
  calls it when it sets up its keyboard handling [verified: `app/Input/InputDispatcher.cs`:51, 99]. G-Helper also uses
  control codes `0x00222400` (registers an event handle) and `0x00222408` (reads an event code) [verified:
  `app/AsusACPI.cs`:266-289]. A fan reader needs none of them. The decompiled firmware's DSTS fan branches do not
  depend on INIT [verified: DSDT 1055-1074].
- **The second argument matters for some ids.** DSTS's bytes 12-15 select a mode when reading fan curves [verified:
  `DeviceGetBuffer`, `app/AsusACPI.cs`:500-507, 634-661]. For the fan speeds keep it 0, as both tools do [verified:
  490-497; asus-wmi.c:568].
- **Sharing.** If another program holds the device with a share mode that conflicts with the access requested,
  CreateFileW fails with ERROR_SHARING_VIOLATION [verified: CreateFileW, dwShareMode]. G-Helper and atrofac both share
  read and write [verified: section 2], and G-Helper's wiki only advises against running it beside Armoury Crate's
  services because both "adjust the same settings" [verified: G-Helper wiki, Requirements, edited 2026-07-22].
  Whether this driver checks share access at all is [unverified].
- **Calls from several programs queue in the firmware.** The decompiled `WMNB` is a `Serialized` method [verified:
  DSDT 714], and Linux serializes its WMI calls with a mutex [verified: asus-wmi.c:360-368]. A slow call from
  lag-doctor would delay ASUS's own service's calls, and theirs would delay lag-doctor's [inferred].
- **Without the ASUS driver.** No `ATKACPI` name, so CreateFileW fails before any control code [inferred; section 2].
  On GitHub's runner (a VM with no ASUS firmware) the device will not exist [inferred]. An ASUS desktop board or old
  laptop may have the device without these ids, and then answers unsupported or absent (section 4) [inferred].
- **Other fan sources on the same laptops.** D3DKMT's adapter perf data has a fan field, which read 0 on the NVIDIA
  laptop GPU measured in `other-hardware.md` section 5.1 [measured: that note], so it is no substitute there.
  NVML's fan query was not checked [unverified]. The same firmware method is also exposed as WMI class
  `AsusAtkWmi_WMNB` in `root\wmi`, whose `DSTS` method takes the device id [verified: G-Helper `app/AsusACPI.cs`:473-488
  (its DEVS); `G14/wmi.txt`:21-26 (DSTS from PowerShell)]; its rights and cost were not checked [unverified].

## 7. What this means for lag-doctor (the research's advice, and what was built)

- A reader under `lagdoctor/winapi/` (ctypes), opened once per recording and closed at its end, read about once a
  second (fans change over seconds; NVML is read once a second too), each round timed with `lagdoctor.clock.now()`,
  so the source's status shows the reader's own cost. Built as `winapi/atkacpi.py` and the `fans` collector
  (`capture/fanmon.py`) on a thread of its own rather than the system sampler's: a slow or stuck firmware call then
  delays only the fan rows, never the 4 Hz CPU, GPU and disk ones.
- One function builds the input, with DSTS fixed in it and the three fan ids as its only allowed ids, the second
  argument 0, and no method parameter (the advice). As built: `atkacpi._call` refuses any buffer but a DSTS read
  of a fan id before DeviceIoControl, and `tests/test_fans.py` checks that no other module under `lagdoctor/`
  names the device (in any spelling), its control code, the set method or the reader's insides, that the reader
  reaches DeviceIoControl only from `_call`, and that a stand-in `DeviceIoControl` saw only DSTS buffers; each
  guard has a red-first sabotage in `tests/redfirst/fans.json`. INIT, WDOG and the event control codes are not
  scanned for: `_call` refuses any method but DSTS, and only its one control code is sent.
- Decoding, strictest of the two sources: check DeviceIoControl's return value and that at least 4 bytes came back
  (else "failed", with the error); then `0xFFFFFFFE` and `0xFFFFFFFF` are "unsupported"; then the high word must be
  exactly `0x0001` (presence bit, no other high bits), else "absent" with the raw value in the reason; then RPM is the
  low word x 100, where `0x00010000` is a real 0 RPM. A failed sample is None, never 0. As built, a speed past
  12,000 RPM (G-Helper's own bound) is a corrupt reading, counted as a failed one.
- On a PC without the device the source is `unavailable`, with the CreateFileW error and "no ASUS System Control
  Interface device", and rules that need fan speed list it as unchecked. A fan the firmware says is absent (many
  laptops have no middle fan) is "absent", not "unavailable".
- RPM, not a percentage: the top speed is per model (section 4). A rule can still say whether the fans were rising,
  flat or at the highest speed this recording saw while the GPU throttled.
- Its cost: 0.35-0.9 ms a call (section 9), two calls a second on a laptop with two fans. Whether a call shows in
  the game's frames is the open question 3: `scripts/overhead/phase_lock.py` has a `fans` source for it.
- As built, the GPU heat finding (`rules._gpu_throttle`) adds one line while heat held the NVIDIA GPU down: each
  fan's median speed over those readings and its fastest in the part analysed. Since 2026-11-19 a second sentence
  judges the CPU and GPU fans within the recording (section 10); it never scores.

## 8. Open questions, with what section 9 answered

1. Does a handle with fewer rights than G-Helper's get DSTS answered? Answered: yes, with no access rights at all
   (section 9), so lag-doctor asks for none. Neither choice makes the calls read-only (section 3).
2. What do the three ids answer, and how many bytes come back? Answered at the desktop: the CPU and GPU fans answer
   with the presence word and 16 bytes; the middle fan's id fails with ERROR_INVALID_FUNCTION and 0 bytes (section 9).
   Under a game: the first recording with the fans collector shows it.
3. How long does one call take, and does a once-a-second poll show in DPC time or frame times? The call: 0.35-0.9 ms
   (section 9). The frames: open until a recording with fans is checked with `scripts/overhead/phase_lock.py
   --sources fans,fans@grid` [unverified].
4. Does the open work unelevated, and from a standard account? Unelevated: yes (section 9). A standard account:
   [unverified].
5. Does the reading still work with ASUS's services stopped? [unverified].

## 9. Measured with lag-doctor's reader (2026-11-17)

One run of a probe built like `winapi/atkacpi.py` (DSTS buffers only, checked before every call), unelevated, on an
ASUS laptop with the ASUS System Control Interface v3, five rounds 0.5 s apart [measured: ctypes, unelevated]:

- The open with access 0 and share read and write succeeded; nothing asked for more.
- CPU fan (`0x00110013`): `0x00010023` every round, 3,500 RPM, 16 bytes returned.
- GPU fan (`0x00110014`): `0x00010024` or `0x00010025`, 3,600 or 3,700 RPM, 16 bytes returned.
- Middle fan (`0x00110031`): DeviceIoControl returned FALSE with error 1 (ERROR_INVALID_FUNCTION) and 0 bytes. This
  laptop has two fans, and its driver refuses the third id rather than answering 0 or an unsupported value, so
  `atkacpi._call` reads error 1 as "no such fan".
- One call took 0.35-0.9 ms, the middle fan's refusal included.

The values fit the units of section 4: hundreds of RPM in the low word under the presence word, and speeds inside
the per-model top speeds G-Helper lists (5,500-9,200 RPM, section 4).

## 10. A yardstick for a fan at full speed (2026-11-19)

Asked whether a recording's fans ran at full speed, so that the cooler and not a fan setting held the GPU back.
Three read-only agents read lag-doctor's fan code, then Linux's asus-wmi driver with two decompiled ROG firmware
tables, and G-Helper's source; a fourth re-read every cited line. Linux at `8dac6df`, G-Helper on main at `aa0037c`
(the fan code is the same at v0.291, `f62d584`), the 2020 Zephyrus G14 table in zllovesuki's reverse_engineering
(`32ad92b`, `G14/g14-dsdt.dsl`) and the ROG Ally table in ruineka's rog-ally-re (`3a0491d`, `acpi/dsdt.dsl`).

- **No firmware call gives a fan's top speed.** Linux defines no such id, and its hwmon files have no `fanN_max`
  [verified: `include/linux/platform_data/x86/asus-wmi.h`:98-105; asus-wmi.c:3372-3386]. Both firmware tables
  answer DSTS in the `0x0011xxxx` range only with speeds, curves, fan floors, a fan-range byte and zeros [verified:
  G14 table 6450-6502, 6526-6538, 6562-6570, 6605-6617; Ally table 8378-8430, 8661-8673, 8727-8735, 8771-8783].
- **The fan mode in force cannot be read back.** DSTS on `0x00120075` returns the constant `0x00010003` on both
  tables; the mode set stores it in the embedded controller, and no DSTS branch reads it [verified: G14 table
  6619-6622, 6960-6981; Ally table 8785-8788, 9501-9555]. Linux and G-Helper keep their own copy of the mode
  [verified: asus-wmi.c:4201-4208, 4324-4344; G-Helper `app/Mode/Modes.cs`:129-143].
- **A fan curve can be read, but not usefully here.** DSTS on `0x00110024`, `0x00110025` or `0x00110032` with a
  curve index in the second dword (0 Balanced, 1 Silent, 2 Turbo, swapped against the mode numbers) returns 16
  bytes, 8 temperatures then 8 duty percents [verified: asus-wmi.c:3618-3634; G-Helper `app/AsusACPI.cs`:635-666;
  G14 table 8897-8912]. It is the mode's factory curve, not the curve in force: the tables' only read command is in
  the routine the reverse engineer named "Get Default Fan Curve", and both tools use it only as defaults [likely:
  G14 table 8897, 8914; asus-wmi.c:3618, 3836-3845]. It is not a pure read: it posts two mailbox commands to the
  embedded controller and can busy-wait about 90 ms [verified: Ally table 11297-11336, 11732-11795]. Linux notes
  TUF models without curve control answer 0 [verified: asus-wmi.c:3485-3507]. It gives duty, not RPM.
- **G-Helper's "top" is its own number.** A per-model table (two main fans 55-78, a middle fan up to 92, in hundreds
  of RPM; 58 for a model it does not list), raised to any faster reading it shows, or set by a calibration that
  writes Turbo and 100 % curves with DEVS [verified: `app/Fan/FanSensorControl.cs`:8, 50-80, 129, 137-187]. A stored
  top is therefore the fastest G-Helper has seen or measured: a lower bound, not a specification.
- **Open: a full-speed flag.** Linux's comment says ASUS's specification puts "Full On or Automatic" in bit 19 of
  the CPU fan's DSTS reply, and that it reads 0 on one laptop [verified: asus-wmi.c:3135-3143]. None of the tables
  sets it [verified: G14 table 6526-6538; Ally table 8661-8673]. `atkacpi.decode` reads a high word other than
  `0x0001` as "no such fan", so a firmware that set it would read a fan at full speed as absent [unverified].

So the verdict rests on the recording itself (`attribute.fan_verdict`, DESIGN.md "Headroom"): each fan's median
while heat held the GPU against the fastest it kept up in the part, and for the CPU and GPU fans the slowest top in
G-Helper's table (5,500 RPM; its CPU and GPU fans run 5,500 to 7,800, a middle fan up to 9,200). "Likely at full
speed" stays a likelihood: a mode whose curve flattens above 5,500 RPM reads the same.

## Verification log (2026-11-17)

Every claim below was read at its source in this research. G-Helper lines are at tag v0.291 (`f62d584`); the fan
code in `AsusACPI.cs`, `FanSensorControl.cs` and `HardwareControl.cs` is identical on main at `aa0037c` (the only
`AsusACPI.cs` changes are a new power id at line 120 and the scan's filter). Linux lines are at `63fdb51`.

| Claim | Result | Source read |
|---|---|---|
| Device path `\\.\ATKACPI` (G-Helper's doubled separator is collapsed by normalization) | verified | G-Helper `app/AsusACPI.cs`:32; atrofac `library/src/atkacpi.rs`:6 (commit `1c7ff3d`); Microsoft "File path formats on Windows systems" (updated 2026-08-04) |
| CreateFileW: read and write rights, share read and write, OPEN_EXISTING | verified in both tools | `app/AsusACPI.cs`:245-250, 298-306; atrofac `device_control.rs`:23-31 |
| Microsoft advises FILE_SHARE_READ and FILE_SHARE_WRITE for device drivers | verified | DeviceIoControl page, Remarks (updated 2024-02-22) |
| CreateFileW fails with INVALID_HANDLE_VALUE; share conflicts give ERROR_SHARING_VIOLATION | verified | CreateFileW page (ms.date 2022-08-16) |
| atrofac's failure check compares with null | verified | `device_control.rs`:33 |
| G-Helper runs as invoker and quits without the device | verified (manifest, startup); that the open works unelevated is inferred | `app/app.manifest`:7; `app/Program.cs`:99-111; `app/Settings.cs`:288 |
| Standard accounts need administrator rights only for power limits, NVIDIA clocks and ASUS services | forum (an open pull request's text) | G-Helper's pull request 6202 |
| `ATKACPI` links to `\Device\ATKACPI` here; a missing name gives error 2; the ASUS2018 device and `ATKWMIACPIIO` | measured, without opening the device | QueryDosDeviceW (ctypes), `Win32_PnPEntity`, `Win32_SystemDriver`, 2026-11-17 |
| Control code `0x0022240C` | verified in both tools | `app/AsusACPI.cs`:33; `atkacpi.rs`:7 (2237452) |
| It decodes to FILE_DEVICE_UNKNOWN, function 0x903, METHOD_BUFFERED, FILE_ANY_ACCESS | verified | "Defining I/O Control Codes" (updated 2025-06-19); SDK 10.0.26100 `um\winioctl.h`:202, 287, 316 |
| FILE_ANY_ACCESS: the IRP is sent for any caller with a handle | verified | "Defining I/O Control Codes" |
| Input: method id, argument length, device id, 0, little-endian, 16 bytes for DSTS | verified | `app/AsusACPI.cs`:410-424, 490-497; atrofac's byte templates `atkacpi.rs`:8-13 |
| Output: 16 bytes in G-Helper, first 4 read as Int32 | verified | `app/AsusACPI.cs`:413, 496 |
| METHOD_BUFFERED uses one buffer of the larger length | verified | "Buffer Descriptions for I/O Control Codes" (updated 2025-07-18) |
| G-Helper ignores DeviceIoControl's return value | verified | `app/AsusACPI.cs`:388-401 |
| Fan ids `0x00110013`, `0x00110014`, `0x00110031` | verified in both sources | `app/AsusACPI.cs`:63-65; `asus-wmi.h`:100-102 |
| DSTS `0x53545344`, DEVS `0x53564544`, INIT `0x54494E49`, unsupported `0xFFFFFFFE` | verified | `asus-wmi.h`:25, 27, 30, 34; `app/AsusACPI.cs`:35-37 |
| RPM = low 16 bits x 100 | verified in both sources and the firmware | `asus-wmi.c`:3107, 3126, 3239, 3241, 3257, 3259; `app/AsusACPI.cs`:496, 569; `FanSensorControl.cs`:132; `HardwareControl.cs`:906-907; DSDT excerpts 1055-1074; `G14/wmi.txt`:21-26 |
| Presence bit `0x00010000`; a fan needs it in Linux | verified | `asus-wmi.h`:173-177; `asus-wmi.c`:654-661, 3462-3472 |
| `~0` is -ENODEV in Linux | verified | `asus-wmi.c`:573-574 |
| G-Helper's -1 for values above 120 or a zero low word without presence | verified | `app/AsusACPI.cs`:570 |
| An absent GPU fan answers `Zero`; an unmatched call answers `0xFFFFFFFE` | verified for one firmware | DSDT excerpts (mist941/zenbook-ux7602zm-linux, `research/acpi/dsdt-excerpts.dsl` at `00c3fdb`), lines 1062-1074, 1748 |
| DCTS instead of DSTS on "ASUSWMI" machines; AGFN before Spec 8.3 | verified | `asus-wmi.c`:231-235, 3460-3466, 5006-5019 |
| TUF laptops "return nothing for unknown methods" | verified (the kernel's comment) | `asus-wmi.c`:5003-5004 |
| DSTS fan branches only read the controller; DEVS fan branches write it | verified for one firmware | DSDT excerpts 828-1237 against 1238-1748, fan branches 1055-1074 and 1510-1545 |
| DEVS on the fan ids sets full speed (1) or automatic (0) | verified | `asus-wmi.c`:3147-3180, 3271-3300, 3317-3345; `G14/wmi.txt`:49-54 (zllovesuki/reverse_engineering at `32ad92b`, 2020-10-18) |
| Linux writes DEVS 0 on the fan ids at start-up | verified | `asus-wmi.c`:2991-3031, 3478 |
| INIT enables hotkeys on some models; G-Helper calls it | verified | `asus-wmi.c`:4975-4977; `app/Input/InputDispatcher.cs`:51, 99 |
| G-Helper's poll rates: 1 s timer, 2 s refresh (6 s hidden), overlay 1 s, CPU temperature cached 2 s | verified | `app/Settings.cs`:250-252, 715, 1641-1645; `app/Overlay/HardwareOverlay.cs`:194, 442; `app/HardwareControl.cs`:451-458, 900-913 |
| G-Helper sweeps DSTS over ids `0x00000000`-`0x001600FF` | verified | `app/AsusACPI.cs`:879-928; its author's request in G-Helper's issue 38 |
| G-Helper's fan calibration writes the mode and fan curves | verified | `app/Fan/FanSensorControl.cs`:137-152 |
| Per-model top speeds (default 5,800 RPM) | verified | `app/Fan/FanSensorControl.cs`:8, 50-80 |
| The ASUS System Control Interface is a Device Manager entry with V3 and V1 drivers | verified (the FAQ does not describe the interface) | ASUS FAQ 1047338 (updated 2025-05-23) |
| G-Helper's wiki: the driver is mandatory; Armoury Crate's services adjust the same settings | verified | G-Helper wiki, Requirements (edited 2026-07-22) |
| A CPU load G-Helper's sensor timer was blamed for | forum | G-Helper's issue 5143 |
| No ASUS document of DSTS or DEVS | unverified (only ASUS's FAQ was read) | none |
| Whether the driver checks share access or handle rights itself; the device's DACL | unverified | section 8 |
| A handle with no access rights reads both fans unelevated; 16 bytes come back; a call takes 0.35-0.9 ms; the middle fan's id fails with error 1 | measured | section 9 |
