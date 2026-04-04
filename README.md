# pwneye

`pwneye` is an offensive security tool for assessing IP cameras that expose **ONVIF** and **RTSP** services.

It is built for offensive security work against IP cameras: discovery, authentication testing, metadata collection, stream validation, and follow-up actions.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Usage](#usage)
- [Authentication Input](#authentication-input)
- [Cache and Recordings](#cache-and-recordings)
- [TODO](#todo)
- [Acknowledgements](#acknowledgements)
- [Safety](#safety)
- [License](#license)

## Features

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

## Installation

### Python

```bash
cd /Users/oblio/Desktop/pwneye
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### External dependencies

The following tools are expected in `PATH` depending on the mode you use:
- `ffplay`
- `ffprobe`
- `ffmpeg` for recording

On macOS with Homebrew:

```bash
brew install ffmpeg
```

## Quick Start

Discover ONVIF cameras on the local network:

```bash
python3 pwneye.py --discover
```

Scan a single target with the default ONVIF + RTSP flow:

```bash
python3 pwneye.py -t 192.168.1.135
```

Try a specific RTSP vendor to reduce requests:

```bash
python3 pwneye.py -t 192.168.1.135 --vendor tenda
```

Use ONVIF wordlists only:

```bash
python3 pwneye.py -t 192.168.1.135 -ou admin -op ~/wordlists/rockyou-short.txt --skip-rtsp --threads 5
```

Use a fixed RTSP password and rotate usernames:

```bash
python3 pwneye.py -t 192.168.1.135 --password 'SuperSecretPass' --vendor hikvision --threads 10
```

Record a validated RTSP stream without preview:

```bash
python3 pwneye.py -t 192.168.1.135 --record engagement-cam.mp4 --no-video
```

Reboot a camera via ONVIF after successful authentication:

```bash
python3 pwneye.py -t 192.168.1.135 --reboot
```

## Usage

### Target selection

You must choose exactly one of:
- `-t, --target TARGET`
- `--discover`

### ONVIF

- `--skip-onvif`: skip ONVIF detection and enumeration
- `-oP, --onvif-port PORT`: test a specific ONVIF port
- `-ou, --onvif-username USER`: ONVIF username or file with one username per line
- `-op, --onvif-password PASS`: ONVIF password or file with one password per line
- `--reboot`: request a reboot via ONVIF and skip RTSP probing

### RTSP

- `--skip-rtsp`: skip RTSP detection and bruteforce
- `-P, --rtsp-port PORT`: test a specific RTSP port
- `-u, --username USER`: RTSP username or file with one username per line
- `-p, --password PASS`: RTSP password or file with one password per line
- `--protocol tcp|udp`: choose RTSP transport, default is `tcp`
- `--timeout SECONDS`: RTSP timeout, default is `10`
- `--vendor VENDOR`: force a vendor from the RTSP database
- `--record [OUTPUT.mp4]`: record the validated RTSP stream; if omitted, a timestamped file is created under `~/.pwneye/recordings`
- `--no-video`: skip live preview and decoding

### Misc

- `--threads N`: number of concurrent threads
- `--no-cache`: do not read from or write to cache
- `--fresh`: ignore cache reads but still write new findings

## Authentication Input

ONVIF and RTSP credential flags accept either:
- a literal value
- a file with one value per line

Examples:

```bash
python3 pwneye.py -t 192.168.1.135 --username admin --password ~/wordlists/passwords.txt
python3 pwneye.py -t 192.168.1.135 --username ~/wordlists/users.txt --password admin123
python3 pwneye.py -t 192.168.1.135 --username admin --password admin
```

Behavior summary:
- fixed `username + password`: only that exact pair is tested
- fixed `username` only: usernames stay fixed, passwords rotate
- fixed `password` only: passwords stay fixed, usernames rotate
- files on both sides: full cartesian product is tested

## Cache and Recordings

Runtime artifacts are stored under `~/.pwneye`:
- `~/.pwneye/cache`: per-target cached ONVIF and RTSP successes
- `~/.pwneye/recordings`: default location for timestamped recordings

Cache behavior:
- default: reuse cached valid findings before running a fresh bruteforce
- `--fresh`: ignore cached results but still update cache with new findings
- `--no-cache`: disable both cache reads and cache writes

Recording examples:

```bash
python3 pwneye.py -t 192.168.1.135 --record
python3 pwneye.py -t 192.168.1.135 --record living-room.mp4
python3 pwneye.py -t 192.168.1.135 --record living-room.mp4 --no-video
```

## TODO

- [ ] Add `--proxy` support to use a bridge machine that can reach cameras on its own local network
- [ ] Allow `--target` to accept a file with multiple IPs/hosts so scans can be parallelized across targets
- [ ] Add support for multi-channel cameras
- [ ] Check for new versions automatically at startup and notify the user when an update is available

## Acknowledgements

Special thanks to [@kaburagisec](https://github.com/kaburagisec) for [`onvif-python`](https://github.com/nirsimetri/onvif-python), the ONVIF library used by `pwneye`.
It made the ONVIF side of this project dramatically easier and more reliable.

## Safety

Use `pwneye` only against assets you own or are explicitly authorized to assess.

This tool can enumerate services, test authentication, open streams, record video, and reboot ONVIF-capable devices.

## License

See `LICENSE`.
