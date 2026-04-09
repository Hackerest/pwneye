<p align="center">
  <img src="assets/logo.png" alt="pwneye logo" width="500">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.0.0-red" alt="version 1.0.0">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey" alt="macOS and Linux">
  <a href="LICENSE.md"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License"></a>
</p>

`pwneye` is a focused and portable offensive security tool for working with IP cameras that expose **ONVIF** and **RTSP** services, aiming to give security researchers and hackers an easy way to handle discovery, authentication testing, metadata collection, stream validation, and follow-up actions from a single CLI workflow.

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

## Table of Contents

- [Installation](#installation)
  - [Python](#python)
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
git clone https://github.com/hackerest/pwneye
cd pwneye
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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

A simple way to approach `pwneye` is:

1. enumerate the local network when you are on the same segment
2. scan a single target with the default ONVIF + RTSP flow
3. narrow RTSP requests with `--vendor` when you already know the device family
4. switch to wordlists only when fixed credentials are not enough

Local network discovery:

```bash
python3 pwneye.py --discover
```

Default single-target workflow:

```bash
python3 pwneye.py -t 192.168.1.135
```

List the RTSP vendors available in the knowledge base:

```bash
python3 pwneye.py --list-vendors
```

If you already know the RTSP vendor, use it early to reduce the number of requests:

```bash
python3 pwneye.py -t 192.168.1.135 --vendor tenda
```

Credential flags accept either:
- a literal value
- a file with one value per line

Examples:

```bash
python3 pwneye.py -t 192.168.1.135 --username admin --password ~/wordlists/passwords.txt
python3 pwneye.py -t 192.168.1.135 --username ~/wordlists/users.txt --password admin123
python3 pwneye.py -t 192.168.1.135 -ou admin -op ~/wordlists/onvif.txt --skip-rtsp
```

Behavior summary:
- fixed `username + password`: only that exact pair is tested
- fixed `username` only: usernames stay fixed, passwords rotate
- fixed `password` only: passwords stay fixed, usernames rotate
- files on both sides: full cartesian product is tested

Global options worth knowing early:
- `--threads N`: number of concurrent threads
- `--no-cache`: do not read from or write to cache
- `--fresh`: ignore cache reads but still write new findings

## ONVIF

### Enumerating the Local Network

Use WS-Discovery to identify ONVIF-capable devices on the local network:

```bash
python3 pwneye.py --discover
```

The discovery loop keeps probing every few seconds, prints only newly discovered devices, and can be stopped with `CTRL-C`.

### Bruteforcing Credentials

Run ONVIF-only bruteforce with a fixed username and a password file:

```bash
python3 pwneye.py -t 192.168.1.135 -ou admin -op ~/wordlists/rockyou-short.txt --skip-rtsp --threads 5
```

Useful options:
- `--skip-onvif`: skip ONVIF detection and enumeration
- `-oP, --onvif-port PORT`: test a specific ONVIF port
- `-ou, --onvif-username USER`: ONVIF username or file with one username per line
- `-op, --onvif-password PASS`: ONVIF password or file with one password per line

`pwneye` caches successful ONVIF credentials per target under `~/.pwneye/cache` and reuses them on future runs unless you use `--fresh` or `--no-cache`.

### Rebooting a Camera

If ONVIF authentication succeeds, you can request a reboot directly:

```bash
python3 pwneye.py -t 192.168.1.135 --reboot
```

When `--reboot` is used, RTSP probing is skipped.

## RTSP

### Identifying the Vendor

`pwneye` will try to identify the RTSP vendor automatically through RTSP banner grabbing before falling back to broader path enumeration.

If you already identified the vendor during a preliminary analysis, you can pass it directly to reduce the number of requests significantly:

```bash
python3 pwneye.py -t 192.168.1.135 --vendor tenda
```

You can also fetch only the RTSP banner and exit:

```bash
python3 pwneye.py -t 192.168.1.135 --skip-onvif --banner
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
python3 pwneye.py -t 192.168.1.135 --username admin --password admin
```

Rotate only usernames with a fixed password:

```bash
python3 pwneye.py -t 192.168.1.135 --password 'SuperSecretPass' --vendor hikvision --threads 10
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
python3 pwneye.py -t 192.168.1.135 --vendor tenda
```

Record a validated RTSP stream with preview:

```bash
python3 pwneye.py -t 192.168.1.135 --record
python3 pwneye.py -t 192.168.1.135 --record living-room.mp4
```

Record without opening the preview window:

```bash
python3 pwneye.py -t 192.168.1.135 --record living-room.mp4 --no-video
```

Recording behavior:
- `--record [OUTPUT.mp4]`: record the validated RTSP stream; if omitted, a timestamped file is created under `~/.pwneye/recordings`
- `--no-video`: skip live preview and decoding
- default recordings are stored under `~/.pwneye/recordings`

## TODO

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

This project is distributed under the MIT License.
See `LICENSE.md`.
