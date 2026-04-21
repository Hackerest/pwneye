<p align="center">
  <img src="assets/logo.png" alt="pwneye logo" width="500">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.0.2-red" alt="version 1.0.2">
  <img src="https://img.shields.io/badge/codename-panopticon-black" alt="codename panopticon">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey" alt="macOS and Linux">
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/license-GPLv3-blue" alt="GNU GPL v3.0"></a>
</p>

`pwneye` is a focused and portable offensive security tool for working with IP cameras that expose **ONVIF** and **RTSP** services, aiming to give security researchers and hackers an easy way to handle discovery, authentication testing, metadata collection, stream validation, recording, and follow-up actions from a single CLI workflow.

Some of the capabilities currently supported include:

- Local network ONVIF discovery via WS-Discovery
- ONVIF authentication with single credentials or username/password files
- Multithreaded ONVIF bruteforce with live progress output
- ONVIF post-auth enumeration of device information, configured users, network configuration, media profiles, and RTSP stream URIs
- ONVIF reboot support with `--reboot`
- RTSP port detection and banner-based vendor identification
- Vendor-aware RTSP bruteforce with exhaustive fallback when needed
- Multithreaded RTSP bruteforce with live progress output
- RTSP stream validation, preview via `ffplay`, and recording via `ffmpeg`
- Per-target caching of successful ONVIF and RTSP findings under `~/.pwneye`

## Demo

https://github.com/user-attachments/assets/6913632b-326d-455e-aa0d-be6bf9b3e66c

## Table of Contents

- [Installation](#installation)
  - [Python](#python)
  - [pipx](#pipx)
  - [External Dependencies](#external-dependencies)
- [Getting Started](#getting-started)
- [ONVIF](#onvif)
  - [Enumerating the Local Network](#enumerating-the-local-network)
  - [Bruteforcing Credentials](#bruteforcing-credentials)
  - [Rebooting a Camera](#rebooting-a-camera)
- [RTSP](#rtsp)
  - [Identifying the Vendor](#identifying-the-vendor)
  - [RTSP Bruteforce](#rtsp-bruteforce)
  - [Streaming and Recording](#streaming-and-recording)
- [TODO](#todo)
- [Acknowledgements](#acknowledgements)
- [Safety](#safety)
- [License](#license)

## Installation

### Python

```bash
git clone https://github.com/Hackerest/pwneye
cd pwneye
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 pwneye.py --help
```

### pipx

Install `pwneye` as a system-wide CLI command from GitHub:

```bash
pipx install git+https://github.com/Hackerest/pwneye.git
pwneye --help
```

Uninstall it:

```bash
pipx uninstall pwneye
```

Upgrade it later from the same GitHub source:

```bash
pipx upgrade pwneye
```

### External Dependencies

The following tools are expected in `PATH` depending on the mode you use:
- `ffplay`
- `ffprobe`
- `ffmpeg` for recording

| Platform | Install command |
| --- | --- |
| macOS (Homebrew) | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt update && sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg ffmpeg-free` |
| Arch Linux | `sudo pacman -S ffmpeg` |
| openSUSE | `sudo zypper install ffmpeg` |

## Getting Started

`pwneye` can be used in different ways depending on what you already know about the target. The examples below are meant to show the fastest way to get value out of the tool, not a single mandatory workflow.

Run the default ONVIF + RTSP flow against a single target:

```bash
pwneye -t 192.168.1.135
```

Discover ONVIF-capable devices on the local network:

```bash
pwneye --discover
```

Use fixed RTSP credentials or wordlists:

```
pwneye -t 192.168.1.135 --username admin --password admin
pwneye -t 192.168.1.135 --username admin --password ~/wordlists/passwords.txt
pwneye -t 192.168.1.135 --username ~/wordlists/users.txt --password admin123
```

Useful flags to keep in mind from the start:
- `--vendor VENDOR`: reduce RTSP requests when the device family is already known
- `--threads N`: control concurrency for ONVIF and RTSP bruteforce
- `--skip-onvif` / `--skip-rtsp`: focus on one protocol only
- `--no-cache`: do not read from or write to cache
- `--fresh`: ignore cache reads but still write new findings

## ONVIF

ONVIF is the management and control side of the camera world. In practice, it is useful for discovery, authentication, metadata extraction, media profile enumeration, stream URI retrieval, and device actions such as rebooting.

In `pwneye`, ONVIF is the protocol that usually gives the richest post-auth context and the cleanest path to understanding what a camera exposes.

### Enumerating the Local Network

Use WS-Discovery to identify ONVIF-capable devices on the local network:

```
pwneye --discover

[info] Starting continuous ONVIF discovery on the local network
[info] Press CTRL-C to stop the probing
[success] Discovered 1 new ONVIF device(s) on the local network
[info] Saved ONVIF discovery data to cache for 192.168.1.135 (Tenda)

   Host: 192.168.1.135
   Port: 80
   Protocol: http
   Types: Device
   XAddrs: http://192.168.1.135:80/onvif/device_service
   Manufacturer: Tenda
   Name: CP3Pro
   Hardware: CP3Pro
   MAC: XX:XX:XX:XX:XX:XX
   Country: China
   Profiles: Streaming
   Capabilities: NetworkVideoTransmitter, ptz, video_encoder, audio_encoder

[success] ONVIF discovery stopped by user after identifying 1 device(s)
```

The discovery loop keeps probing every few seconds, prints only newly discovered devices, and can be stopped with `CTRL-C`.

### Bruteforcing Credentials

Run ONVIF-only bruteforce with a fixed username and a password file:

```
pwneye -t 192.168.1.135 -ou admin -op ~/wordlists/rockyou-short.txt --skip-rtsp --threads 5

[info] Checking if the target (192.168.1.135) is reachable...
[info] The target seems to be reachable
[info] Trying ONVIF authentication using user-provided credentials...
[success] 192.168.1.135 supports ONVIF on port 80
[warning] Unable to authenticate via ONVIF using provided credentials
[>] Do you want to extend the test to common ONVIF credentials? [(y)es/(n)o] (default: y): 
[info] No explicit ONVIF credentials specified, trying common ONVIF credentials...
[info] Trying ONVIF authentication using common username(s) and password(s)...
[success] 192.168.1.135 supports ONVIF on port 80
⠼ Trying ONVIF on 192.168.1.135:80 with camera:12345

```

Useful options:
- `--skip-onvif`: skip ONVIF detection and enumeration
- `-oP, --onvif-port PORT`: test a specific ONVIF port
- `-ou, --onvif-username USER`: ONVIF username or file with one username per line
- `-op, --onvif-password PASS`: ONVIF password or file with one password per line

`pwneye` caches successful ONVIF credentials per target under `~/.pwneye/cache` and reuses them on future runs unless you use `--fresh` or `--no-cache`.

### Rebooting a Camera

If ONVIF authentication succeeds, you can request a reboot directly:

```
pwneye -t 192.168.1.135 --reboot

[info] Found cached ONVIF/RTSP credential(s) for 192.168.1.135
[info] Checking if the target (192.168.1.135) is reachable...
[info] The target seems to be reachable
[info] Trying cached ONVIF credentials for the target...
[success] 192.168.1.135 supports ONVIF on port 80
[success] ONVIF connection established using the following configuration:

   Port: 80
   ONVIF Username: admin
   ONVIF Password: Hackerest1

[info] Using previously cached ONVIF credentials
[warning] Requesting ONVIF system reboot...
[success] ONVIF reboot request accepted. The camera is rebooting.
```

When `--reboot` is used, RTSP probing is skipped.

## RTSP

RTSP is the streaming side of the camera world. It is the protocol that usually gives you the live video path, but it is also the most fragmented one: vendors use different paths, channel conventions, authentication quirks, and banner formats.

In `pwneye`, RTSP handling is built around port discovery, banner grabbing, vendor-aware path selection, bruteforce orchestration, stream validation, preview, and recording.

### Identifying the Vendor

`pwneye` will try to identify the RTSP vendor automatically through RTSP banner grabbing before falling back to broader path enumeration.

If automatic banner-based identification fails and you already know the vendor from prior analysis, you can pass it directly to reduce the number of requests significantly:

```bash
pwneye -t 192.168.1.135 --vendor tenda
```

You can also fetch only the RTSP banner and exit:

```
pwneye -t 192.168.1.135 --skip-onvif --banner

...
[info] RTSP service detected on port(s): 554
[success] RTSP banner on port 554: Hipcam RealServer/V1.0
```

Useful options:
- `--skip-rtsp`: skip RTSP detection and bruteforce
- `-P, --rtsp-port PORT`: test a specific RTSP port
- `--vendor VENDOR`: force a vendor from the RTSP database
- `--list-vendors`: print the vendors supported by the RTSP knowledge base and exit
- `--protocol tcp|udp`: choose RTSP transport, default is `tcp`
- `--timeout SECONDS`: RTSP timeout, default is `10`

### RTSP Bruteforce

Bruteforce RTSP with fixed credentials:

```bash
pwneye -t 192.168.1.135 --username admin --password admin
```

Rotate only usernames with a fixed password:

```bash
pwneye -t 192.168.1.135 --password 'SuperSecretPass' --vendor hikvision --threads 10
```

Useful options:
- `-u, --username USER`: RTSP username or file with one username per line
- `-p, --password PASS`: RTSP password or file with one password per line
- `--threads N`: number of concurrent threads used by the bruteforce engine

`pwneye` caches successful RTSP credentials and validated stream metadata per target under `~/.pwneye/cache`.

Cache behavior:
- default: reuse cached valid findings before running a fresh bruteforce
- `--fresh`: ignore cached results but still update cache with new findings
- `--no-cache`: disable both cache reads and cache writes

### Streaming and Recording

Open a validated stream with live preview:

```bash
pwneye -t 192.168.1.135 --vendor tenda
```

Record a validated RTSP stream with preview:

```bash
pwneye -t 192.168.1.135 --record
pwneye -t 192.168.1.135 --record living-room.mp4
```

Record without opening the preview window:

```
pwneye -t 192.168.1.135 --record living-room.mp4 --no-video

...
[info] Recording RTSP stream to /Users/user/.pwneye/recordings/recording_2026-04-14_20-25-03.mp4
[info] Press CTRL-C to stop the recording
[warning] Retrying MP4 finalization in compatibility mode (transcoding)...
[success] Recording saved to /Users/user/.pwneye/recordings/recording_2026-04-14_20-25-03.mp4 (5.75 MB)
```

Recording behavior:
- `--record [OUTPUT.mp4]`: record the validated RTSP stream; if omitted, a timestamped file is created under `~/.pwneye/recordings`
- `--no-video`: skip live preview and decoding
- default recordings are stored under `~/.pwneye/recordings`

## TODO

- [ ] Add `--snapshot` support to capture a still frame from a validated RTSP stream
- [ ] Allow `--target` to accept a full RTSP connection string, so ONVIF discovery can be skipped and credential testing can run directly against a known stream path
- [ ] Add `--connection-strings` / `-cs` to manually provide one or more RTSP connection strings during testing
- [ ] Add SOCKS proxy support so `pwneye` can operate through a bridge or pivot host that can reach cameras on its own local network
- [ ] Allow `--target` to accept a file with multiple IPs/hosts so scans can be parallelized across targets
- [ ] Add support for multi-channel cameras
- [ ] Check for new versions automatically at startup and notify the user when an update is available
- [ ] Improve the RTSP database by expanding and refining vendor banner fingerprints

## Acknowledgements

Special thanks to [@kaburagisec](https://github.com/kaburagisec) for [`onvif-python`](https://github.com/nirsimetri/onvif-python), the ONVIF library used by `pwneye`.
It made the ONVIF side of this project dramatically easier and more reliable.

## Safety

Use `pwneye` only against assets you own or are explicitly authorized to assess.

This tool can enumerate services, test authentication, open streams, record video, and reboot ONVIF-capable devices.

## License

This project is distributed under the GNU GPL3 License.
See `LICENSE.md`.
