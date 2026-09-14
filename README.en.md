# VibeMote · Google TV Remote → Windows Input Device

🌐 **English** | [中文](README.md)

Turn a **Google Chromecast / Google TV Bluetooth remote** into a "push-to-talk + hotkey panel"
device for Windows: **hold the remote's voice button and talk → remote microphone → virtual audio
cable → your IME types the text**. 14 buttons are freely remappable —
**Up/Down = wheel, Left/Right = switch conversation, YouTube / NETFLIX = approval Approve / Deny**.

**Download → unzip → double-click `启动遥控器.vbs`.** There is no fourth step (the offline package
bundles Python, so the target machine needs nothing installed).

> ⬇️ **[Download the latest release](releases/latest)**
> · `vibe-mote-offline-v1.zip` (~61MB, **bundles Python + everything** ← recommended)
> · `vibe-mote-offline-voice-v1.zip` (~14MB, **voice only, no frida** ← pick this for AV-wary or work PCs)
> · `vibe-mote-v1.zip` (~900KB, for machines that already have Python 3.10+)
> Changelog → [`CHANGELOG.md`](CHANGELOG.md) ·
> Want an AI to install it for you → give it the repo URL together with [`AGENTS.en.md`](AGENTS.en.md)

> ⚠️ **Read this before downloading (the 30-second version)**
> **Voice** (hold a button, get text) needs no administrator rights and injects nothing — every package above does it.
> **Key mapping** (D-pad/OK/volume) works by **injecting a module into the system's Bluetooth HID driver host**: it needs
> **one** administrator approval (never prompts again after "One-click optimization"), it may be **flagged or silently
> quarantined by antivirus**, a few games with **kernel anti-cheat** may refuse to start, and a **work/school PC** may
> block it outright.
> → Voice only: take the `-voice` package (no frida, smaller) and **never** click "Install key-mapping dependencies".
> → Want key mapping: the full package already ships frida — just click "Install key-mapping dependencies". Adding frida
> to the `-voice` package later has two routes — **① online**: click the same button (Chinese mirrors, ~47MB download);
> **② offline**: drop the `frida-*-win_amd64.whl` into the program folder's `wheels\` directory and click it (bundled
> wheels are preferred). Either way the first attach needs **one** administrator approval (or run "One-click
> optimization" first).
> Full list in [section 5.5](#55-known-limits-and-risks-better-said-up-front-than-hidden-in-a-corner).

The home page is a **"First-time setup" checklist** — what each of the five items is, whether it is
required or optional, its current state, and the button to fix it, all on one screen. Work down the
list once and you are done.

| Remote button | Default action |
|---|---|
| **Voice button** (the one with the mic) | **Push-to-talk** — remote microphone → virtual audio cable → input method editor types the text |
| Up / Down | Mouse wheel up / down (**hold to keep scrolling**; a tap scrolls one notch) |
| Left / Right | Switch to previous / next conversation (preset, changeable) |
| OK / Back / Home / Power / Input source / Mute / YouTube / NETFLIX / Volume ± | Freely remappable in the web UI |
| YouTube / NETFLIX | Approval **Approve / Deny** (`Enter` / `Esc`) |

Web console (`127.0.0.1:8787`): status · key mapping (with a photo of the actual remote) · voice · settings · logs.
Supports **presets**: pick a "target application (WorkBuddy / Codex / Claude Code / generic) + input method editor (Qianwen / Sogou / WeChat / built-in Windows)", and the whole keymap is configured in one click.

Remapping happens on this page: click any button on the remote picture on the left, change what it
sends on the right. Presets configure a whole keymap in one click, "restore recommended" undoes it,
and mappings can be exported / imported.

---

> 🌐 **Bilingual**: switch languages in the top-right corner of the UI (it auto-detects from your
> browser language on first open, and falls back to Chinese). Chinese docs:
> [`README.md`](README.md), [`说明.md`](说明.md) and [`AGENTS.md`](AGENTS.md).
> The messages the backend produces for the UI (dependency notes, presets, cleanup list,
> environment check) are bilingual too; low-level diagnostic logs stay in Chinese.
> Looking for the long illustrated guide? → [`说明.en.md`](说明.en.md)

## 1. What makes this build clean

- ✅ **No drivers installed** (no virtual HID, no kernel filter driver, doesn't touch HidHide)
- ✅ **The voice part needs no administrator privileges.** Key mapping (optional) needs **one** elevation — Frida has to inject into the Bluetooth HID driver host and register a temporary helper service; after clicking "**One-click optimization**" a highest-privilege scheduled task does it for you, **and it never prompts again**. The other prompt is installing the virtual audio cable (that is its own installer's requirement). Details in section 5.5 below
- ✅ **No console windows flash**: single process, launched with the window hidden throughout, and nothing flashes on autostart either
- ✅ **No test mode, no disabling Secure Boot, no system file changes**
- ✅ **The UI only listens on `127.0.0.1`**, so it never triggers a firewall prompt
- ✅ **Very little code**: 9 source files, **only 3 third-party dependencies** (not even numpy is needed)

---

## 2. Dependency list (what to install, which versions)

### Runtime environment

| Item | Requirement | Notes |
|---|---|---|
| OS | **Windows 10 / 11 x64** | Uses the WinRT Bluetooth API and SendInput |
| Python | **3.10 or newer** | Installer from python.org; **be sure to check `Add python.exe to PATH`** during setup |
| Virtual audio cable | **VB-CABLE** (VB-Audio Virtual Cable) | Prerequisite for voice. Installing it needs administrator rights once; **a reboot afterwards is recommended** |

> With the **offline full bundle**, neither Python nor any of the packages below are your concern — everything is already installed inside.

### Python packages (`requirements.txt`)

| Package | Version | Purpose |
|---|---|---|
| `bleak` | **3.0.2** | Bluetooth GATT: voice-button state channel + remote microphone audio |
| `sounddevice` | **0.5.6** | Audio output/input (PortAudio binding); routes the remote microphone into the virtual audio cable |
| `cffi` | ≥1.16 | A `sounddevice` dependency, pulled in automatically |

### Key-mapping dependencies (`requirements-keys.txt`, **optional**)

| Package | Version | Purpose |
|---|---|---|
| `frida` | ≥17.18 <18 | **Required for key mapping** (~130MB): injects into the Bluetooth driver host to read HID reports. Installed into the bundled `.venv`, exits as soon as it is done |

> **Everything works fine without frida** — the voice button uses the ATVV channel and does not depend on it. Only the D-pad/OK mappings are unavailable.
> Installing frida triggers one UAC prompt on first attach (Frida installs a helper for itself); after running this project's "one-click optimization",
> autostart switches to a highest-privilege scheduled task, and **it never prompts again**.

### What is explicitly **not** needed (don't be misled by older guides)

❌ WinUHid / virtual HID driver　❌ Test signing / disabling Secure Boot　❌ HidHide　❌ Interception driver　❌ A resident administrator process

---

## 3. Installation

### Route 0: the easiest way (two steps for a brand-new user)

```
1) Double-click "启动遥控器.vbs"  → the UI opens by itself (no console window)
2) Click "Install all" in the top banner → then "Restart client" → done
```

**No command line at all**, and installing dependencies needs no administrator either (Frida's **first attach** prompts once — see section 5.5). The program automatically picks a Chinese mirror to download from
(Tencent Cloud → USTC → Aliyun → Huawei Cloud → official), installing into a `.venv` inside the program directory,
without polluting the system Python.

> Only Python has to be installed by you first ([python.org](https://www.python.org/downloads/),
> **be sure to check `Add python.exe to PATH`** during setup).
>
> **Don't want to install Python at all?** (Or need to copy it to someone else's machine on a USB stick?) → use "Route D: offline full bundle" below,
> which bundles Python and runs on a machine with nothing installed by double-clicking.

On first opening the UI, the banner tells you exactly what is missing and which button to click; the dependency installation **progress automatically jumps to "Settings → Diagnostics" and scrolls into view**.

### Route A: manual (3 steps)

```bat
:: 1) Enter the program directory, create a virtual environment (doesn't pollute system Python, no admin needed)
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

:: 2) Only needed for key mapping (~130MB, optional)
.venv\Scripts\python -m pip install -r requirements-keys.txt

:: 3) Launch
double-click 启动遥控器.vbs
```

Clicking "Install base dependencies / Install key-mapping dependencies" in the UI does the same thing — **the program polls the Chinese mirrors automatically**.

### Route B: let an AI install it for you

Send **this repo's URL** together with the text below to your usual AI (ChatGPT / Claude / Doubao / WorkBuddy all work):

> Please read `AGENTS.en.md` in `<repo URL>` and follow its steps to install this remote-control tool on my Windows machine.
> My system is Windows 11. If anything is unclear, ask me first; do not install drivers or change system settings on your own.

`AGENTS.en.md` is an execution manual for an AI: precise steps, verification commands, failure-troubleshooting table, and forbidden actions.
Following it requires no Python knowledge from the user.

### Route C: offline / USB-stick deployment (no network, or don't want one)

On a machine **with internet access**, download the dependencies as wheels, put them in `wheels\`, and copy the whole thing over:

```bat
python -m pip download -r requirements.txt -d wheels
python -m pip download -r requirements-keys.txt -d wheels    :: optional, ~130MB
```

The program **uses the `wheels\` directory whenever it detects one** (`--no-index --find-links`), with no network access at all.
The package itself is under 1MB.
> This route still requires **Python on the target machine** (wheels are only packages, not an interpreter).

### Route D: offline full bundle (works without Python / for copying to someone else on a USB stick)

`vibe-mote-offline-v1.zip` (~61MB) **bundles a Python runtime + pre-installed dependencies**:

```
On the other machine: unzip → double-click "启动遥控器.vbs" → use it
(no Python install, no network; key mapping still needs that one elevation — see section 5.5)
```

What is inside (and why it is so large):

| Component | Size | Notes |
|---|---|---|
| Python 3.13 official **embeddable** package | 10.4MB | The official distribution meant for "embedding into an application and running directly" |
| bleak / sounddevice / cffi / winrt-* | ~5MB | Required for voice |
| frida | 47.3MB | Required for key mapping (this is the bulk of the bundle) |

> The two "Install dependencies" buttons in the UI tell you directly on the offline bundle that "dependencies are bundled, nothing to install".

> 📌 **The bundled `runtime\` is part of the program itself; the cleanup tool will not delete it.**
> That is because "Install all" in the UI installs pip packages and **cannot reinstall** a Python distribution —
> once deleted, you can never get back to the "no-install" state. So the cleanup list simply does not offer that option.

---

## 4. Three things to do the first time

**The top of the home page has a “First-time setup” checklist** that shows the state and a button for each of
these five items, so you can just work down it (no digging through the "Settings" page):

| Row | Required? | In one line |
|---|---|---|
| Runtime dependencies | **required** | bleak / sounddevice (voice) + frida (key mapping). Click "Install all" |
| Virtual audio cable | **required** | the physical prerequisite for voice. Click "Install" |
| Qianwen "allow injection" | optional | only needed if you use the Qianwen IME; the program turns it on automatically on first launch |
| Launch at startup | optional | one-click toggle, no administrator needed (to remove Frida's UAC too, click "One-click optimization" as well — that one needs administrator) |
| One-click optimization (no more UAC) | optional | after this, Frida stops prompting for elevation |

The manual steps it corresponds to:

1. **Pair the remote**: Windows Settings → Bluetooth & devices → Add device → hold the remote's **Back + Home** buttons together to enter pairing mode
2. **Install VB-CABLE**: click the **install** button on the home-page checklist or on the **Virtual audio cable** card in "Settings" — it runs the bundled official installer if there is one, otherwise it downloads it from the official site first (~1.3MB).
   - ⚠ **One UAC prompt appears** (that is the audio driver installer's own requirement, unrelated to this program), then **click “Install Driver” in its own window**, and finally **reboot once** as VB-Audio requires.
   - **On first launch the program starts this step for you automatically** (it only auto-tries once — if you decline the UAC it will not nag you again). **The copy started by autostart never prompts** (the autostart entry passes `--boot`): at boot you are not at the machine, so the prompt would just sit there — and it would appear on every single boot.
   - The official installer **offers no silent-install switch** (it has no `-i` / `-s` / `/S`), so "automatic" only saves you from hunting for the button; the click and the reboot cannot be skipped.
3. **Select the microphone in your input method editor**: choose **`CABLE Output (VB-Audio Virtual Cable)`**
   (change it only there; keep your real microphone as the system default recording device)

> **Qianwen IME users need to do nothing**: Qianwen discards injected keystrokes by default, so on **first launch the program
> automatically turns on** the "allow injection" switch (writes a user environment variable and restarts the Qianwen process once; no administrator needed).
> That switch can be toggled manually on the "Voice" page at any time — if you deleted it under "Cleanup", the program remembers
> and will not add it back automatically; click that button to restore it.

---

## 5. What it changes on your computer (the complete list)

This is the most common question, so here is everything — **the program itself is a purely portable
deployment**: no drivers, nothing written to system directories, no resident administrator
(**key mapping needs one elevation, and its limits/risks have their own section 5.5**).
This is the *complete* list of what it touches:

### 1. Files inside its own program folder (delete the folder and they are gone)

`config.json` (config) · `client.log` (log) · `uninstall.log` · `qwen_optout` (the marker that you deleted
Qianwen's switch) · `cable_autotried` (the marker that it auto-tried the cable install) · `_ready.txt`
· `_deferred_result.txt` · `_uninstall_result.json`

### 2. System settings — **every one of them is something you clicked, and every one can be undone**

| What | When it happens | How to undo |
|---|---|---|
| `HKCU\...\CurrentVersion\Run\VibeMote` | **only** when you turn on "Launch at startup" | turn it off in the UI, or tick that item in cleanup |
| The highest-privilege scheduled task `VibeMote` | **only** when you click "One-click optimization" | tick "Disable autostart (scheduled task)" in cleanup |
| `HKCU\Environment\QIANWEN_IME_UTILITY_VOICE_HOOK_ALLOW_INJECTED=1` | written automatically when the Qianwen IME is installed (you can opt out) | the UI button, or tick that item in cleanup |
| Frida's temporary helper services `frida-<pid>-x86*` | registered by **Frida itself** while attaching, not by us | tick that item in cleanup; after "One-click optimization" they are not needed |

**What it never does**: install drivers, touch `System32` / `Program Files`, change your IME settings in
the registry, enable test mode, touch Secure Boot, disable Defender, run as a resident administrator.
It only uses the network in two places: when you click "Install dependencies", and when you click
"Install the virtual audio cable" while no installer is bundled (it downloads from the VB-Audio site).

### 3. The only two things that reach deep into the system are **performed by third-party installers — the program only launches them**

| What | Who installs it | What it needs |
|---|---|---|
| **VB-CABLE** virtual audio cable (a kernel audio driver) | **VB-Audio's own installer** | one UAC prompt + a reboot |
| **Frida**'s helper process | Frida's own mechanism | one UAC prompt on the first attach |

> ⚠ **One thing you should know up front: VB-CABLE's official uninstaller does not fully uninstall.**
> After clicking "Remove Driver" and rebooting, the driver **may still be there** (Device Manager shows it
> fine and all audio endpoints reappear). The reason is that it leaves the driver package in the
> DriverStore, `C:\Windows\inf\oem634.inf`, and a few service keys — and Windows reinstalls them from
> those on boot.
> To remove it for real, run one more command as administrator:
> ```
> pnputil /delete-driver oem634.inf /uninstall /force
> ```
> (`oem634.inf` differs per machine — find it with `pnputil /enum-drivers`, looking for the entry whose
> `Original Name` is `vbmmecable64_win10.inf`.)
> That is VB-Audio's uninstaller's behaviour, not this program's — but the UI states it plainly instead
> of pretending the removal succeeded.

### 4. Can it break someone else's computer?

No. There is no kernel driver, no resident service of its own (Frida's attach registers a temporary
**helper service**; after "One-click optimization" even that is not needed) and nothing written to system
directories; the worst case is "one feature does not work, and the log says why". The two things worth care
are listed above — VB-CABLE's install/uninstall needs UAC and a reboot (its own requirement) — and the
cleanup ticks **nothing by default**: only what you tick gets removed.

> Also note: **only one client can use this remote at a time** (the remote only gives the first client its
> full service table). If another tool of the same kind is running on your machine, this program will
> report "ATVV characteristic missing". Close the other client first.

### 5.5 Known limits and risks (better said up front than hidden in a corner)

**Key mapping (D-pad/OK/volume) and voice are two different things, with different costs:**

| | Voice (talk into the remote → text) | Key mapping (14 buttons → keyboard/mouse) |
|---|---|---|
| Needs administrator? | **Not at all** | **One elevation**: Frida injects into the Bluetooth HID driver host and registers a temporary service |
| How it works | an ordinary Bluetooth GATT connection | hooks that driver host **in memory** (gone after a reboot) and erases the native action of the **mapped keys only** |
| Without frida installed | works as usual | this feature is unavailable; everything else is fine |

- **Blast radius**: it injects into the **Bluetooth HID driver host**, which also serves your other Bluetooth
  keyboards/mice. We only rewrite the reports of the mapped keys, but if that host ever crashed your Bluetooth
  input would drop with it (never seen in practice; if it happens, restart the client or re-plug the adapter).
- **Undocumented interfaces**: key mapping relies on an **undocumented IOCTL** of the Windows Bluetooth HID
  driver and one registry path, and pins `frida >=17.18,<18`. A Windows or frida update can silently break it —
  it degrades gracefully: voice keeps working, key mapping is disabled and the log says so.
- **Antivirus false positives**: frida's module names (`frida-agent.dll`, `frida-helper-*.exe`) are in several
  vendors' signature sets. The offline bundle is ~61MB, ~47MB of which is frida — if your AV silently quarantines
  it, key mapping will simply be broken after extraction (and it looks like our bug). Symptom: `keys.ready`
  stays false after "Install key-mapping dependencies", or the log says frida is missing. Fix: whitelist the
  program folder, or reinstall the dependency. We will **never** ask you to disable Defender or turn on test mode.
- **Protected games**: injecting into a SYSTEM process looks a lot like a cheat loader to anti-cheat. A few games
  with kernel anti-cheat may refuse to start — the easy answer is to **quit the client before gaming** (or at
  least don't use key mapping then).
- **Licence**: frida is wxWindows 3.1 (LGPL-2.1+ exception); bundling it is compliant, see
  [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

---

## 6. Download sources (installable in China without a VPN)

The program has built-in **multi-source automatic failover**: when one fails it moves to the next.

```
bundled wheels (fully offline) → Tencent Cloud (腾讯云) → USTC (中科大) → Aliyun (阿里云) → Huawei Cloud (华为云) → TUNA (清华) → official PyPI
```

This order is tried automatically; in the normal case the first source finishes the job and you never
have to think about it.

> Two notes:
> · **TUNA (清华) returns an empty index** (`from versions: none`), so it is last — don't pick it manually;
> · **NetEase 163 is not included**: its PyPI mirror stopped syncing long ago, so using it would install an outdated version.
> To change sources: edit the `PIP_MIRRORS` table at the top of `app.py`.

---

## 7. Troubleshooting

"Settings → **Environment check**" in the UI tells you item by item which layer is broken (dependencies / sound card / Bluetooth / permissions / autostart / Qianwen switch).
The "Logs" page is a live stream; when reporting a bug, just paste that section.

| Symptom | Cause | Fix |
|---|---|---|
| Logs are flooded with `connection timeout` | The remote is asleep | **Press any button on the remote**; it reconnects within 3 seconds |
| Logs are flooded with `missing ATVV characteristic` | Another client is holding the Bluetooth channel | Close the other client, then click "Restart" |
| **Every microphone level reads 0 / no audio gets in** (both the laptop mic array and the remote) | **The system default recording device was set to `CABLE Output`** and nothing feeds CABLE Input, so every program that uses the default microphone receives pure silence | Set the default input back to the **real microphone** under Settings → System → Sound → Input (the virtual cable should only appear in the **IME tool own** microphone setting). Click Settings → Diagnostics → **Microphone self-check**: it prints the default device and the measured peak, which shows immediately whether this is the cause |
| Pressing the voice button does nothing | Qianwen's "allow injection" is off | Click that button on the "Voice" page |
| **D-pad / OK / volume do nothing**, and the log keeps printing `Attach failed: TransportError: the connection is closed` | **Antivirus / EDR is blocking the injection** (**by far the most common case on a corporate PC**: the security software cuts Frida's injection channel) | Ask **IT to whitelist the program folder** (we never change system settings or disable Defender). Until then **voice keeps working** — only key mapping is unavailable. Click "Settings → Diagnostics → Key-mapping self-check" and it writes the evidence into the log |
| The log keeps printing `GATT Protocol Error: Insufficient Authentication` (or `missing ATVV characteristic`) | The remote's link was never properly **paired/encrypted** — this BLE peripheral insists on an encrypted link | Settings → Bluetooth & devices → **Remove device**, then hold 【**Back + Home**】 on the remote to pair it again, then click Restart. No drivers, no reinstall |
| No sound right after boot | The audio service / sound card became ready later than the program | **Wait 5–10 seconds** (the program retries every 5 seconds and reconnects on its own) |
| The log keeps printing `missing ATVV characteristic` and the UI shows an old config | Most likely **two instances** are alive, fighting over Bluetooth | Click "Restart the client" — it now **stops every old instance from this folder** (not just whichever holds the port) and keeps only the new one |
| Installing the virtual audio cable reports `Unknown Error 1072` | That service is stuck in the "marked for deletion" state (an earlier leftover cleanup deleted the service without stopping the driver first) — its "run as administrator" hint is misleading, you already are | **Reboot once**, then click "Install the virtual audio cable" again; if that still fails, right-click `vbcable\VBCABLE_Setup_x64.exe` → "Run as administrator" and click Install Driver |

The program writes its startup log to `client.log` in the same directory; if startup fails, the launcher shows a prompt and opens it automatically.

---

## 8. Uninstalling

Two steps, in this order:

1. In the UI, go to "Settings → Cleanup / Uninstall" and click "Run 'recommended cleanup'" → cleanup starts (disables autostart + removes Frida leftovers/services + deletes dependencies);
2. Then **just delete the entire program directory**.

The program is a purely portable deployment (no installer, nothing written to system directories), so after step 2 nothing remains on the system.

**The virtual audio cable is not covered by those two steps — it needs a note of its own.** The cleanup list has an
"Uninstall the virtual audio cable (VB-CABLE)" item, but it is **unticked by default and not part of "recommended cleanup"** —
because it is a **system-wide audio driver** that OBS, voice changers and streaming tools may also be using. If you do want it gone:

- tick it → cleanup prompts for UAC once and **launches the official uninstaller**, in whose window you still have to click **"Remove Driver"**;
- as VB-Audio requires, you **must reboot** afterwards to finalise it.
- It is therefore the only item in the list that still needs a human click *and* a reboot; the log says so explicitly.

**If installing/uninstalling gets stuck** (the registry says it is installed but no device is enumerated, and
"Install Driver" does nothing), the cleanup list also has **"Clear virtual-audio-cable leftovers (registry / driver
store)"**, unticked by default too. It only removes VB-CABLE's own entries (the two service keys,
`HKLM\SOFTWARE\VB-Audio\Cable`, the "Apps & features" entry, the driver package in the DriverStore, and
`C:\Program Files\VB\CABLE`) and **never touches Voicemeeter's keys or folders**, even though Voicemeeter shares the
VB-Audio vendor key. Reboot once afterwards, then click "Install the virtual audio cable" again.
(When the yellow "the virtual audio cable is not usable right now" banner shows up on the home page, that button is
right next to it.)

---

## 9. License

**MIT**, see [`LICENSE`](LICENSE). Personal use, study, modification and redistribution are all fine.

> For **commercial use** (selling it, integrating it into a commercial product, or offering a commercial service with it), please contact the author for authorization first.
> This is a request from the author, not an additional clause of MIT — the terms of the MIT license remain unchanged.

Licenses of the third-party components in the packaged distribution are in [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md).

---

## If it works for you

Everything here — from the first line of code to the packaging — was written by one person. There are
**no ads, no telemetry, and no data collection of any kind**. If it saved you some time, a ⭐ is the
most concrete way to say thanks — and it helps the next person stuck with the same remote and the same
window-switching find it.

If something went wrong during setup, or your remote / IME behaves differently, please open an issue
(just paste the lines from the Logs page — that is what troubleshooting runs on).

---

## 9. Choosing a remote (genuine / compatible / clone)

This tool needs **two capabilities** from the remote, and they do **not** always come together:

| Capability | Used for | How to check |
|---|---|---|
| **ATVV voice service** (`ab5e0001`, audio `ab5e0003`) | hold to talk → text | the log does not say "missing ATVV characteristic" ✓ |
| **Standard HID key reports** (3-byte Consumer Control) | D-pad / OK / volume mapping | the log line "key mapping: attached to driver host … ← device `…_Dev_VID&0218d1_PID&9450_…`" |

**Measured on 2026-09-15 with two units both advertised as "G9N9N / GA01919"**:

- one works: it reports **VID `18D1` / PID `9450` / REV `011b`** (identical to the genuine one) — voice and keys both work ✓
- the other fails: GATT returns `Insufficient Authentication` and the service table has **only the four generic services** (no `ab5e…`) → voice can never work ✗; **replacing it with the same model fixed everything** ✓

So: **compatible units from the same listing and the same "model number" can differ a lot between batches**. When buying:

1. ask the seller "does it support **voice (ATVV)**?" and "is it a genuine G9N9N / GA01919?";
2. when it arrives, look at the **device identity** line in the log (`VID&0218d1_PID&9450` matches the genuine one ✓);
3. if the log itself says "this remote has **no voice service**" → do not suspect the software, **return it**.

**Known good**: the genuine **G9N9N** (Chromecast with Google TV voice remote) and the
**Google TV Streamer (2024) remote** (Google says it works with 2020+ Chromecasts, but this project has
**not tested it**). **Risky**: the ~¥15 "for Google" compatible units — many work, but batches vary; use
the three steps above.