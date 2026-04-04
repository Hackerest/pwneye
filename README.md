# pwneye

**pwneye** is an offensive security tool for assessing IP cameras that expose **ONVIF** and **RTSP** services.

It is designed for pentesters, red teamers, internal security teams, and researchers who need a practical way to:
- identify ONVIF-capable cameras on a local network
- brute-force ONVIF and RTSP authentication in a controlled way
- enumerate device metadata, users, media profiles, and RTSP stream URIs
- validate working streams and open them with `ffplay`
- record validated RTSP streams to disk
- cache successful findings for faster follow-up assessments

pwneye is built for **authorized testing only**.

## Features

- ONVIF discovery on the local network via WS-Discovery
- ONVIF authentication with single credentials or wordlists
- ONVIF multithreaded bruteforce with live progress output
- ONVIF post-auth enumeration of device information, configured users, network configuration, media profiles, and RTSP stream URIs
- ONVIF reboot support with `--reboot`
- RTSP port detection and RTSP banner grabbing
- Vendor-aware RTSP bruteforce using the built-in knowledge base
- Exhaustive RTSP path fallback when vendor identification fails or vendor-specific paths do not work
- RTSP multithreaded bruteforce with live progress output
- RTSP preview via `ffplay`
- RTSP recording via `ffmpeg`
- Per-target caching of valid ONVIF and RTSP findings under `~/.pwneye`

## Installation

### Python requirements

pwneye targets Python 3 and depends on the packages listed in `requirements.txt`.

A typical setup looks like this:

```bash
cd /Users/oblio/Desktop/pwneye
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### External dependencies

pwneye also expects the following tools to be available in `PATH` depending on the mode you use:
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

Use wordlists against ONVIF:

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

## CLI Overview

### Target selection

You must choose exactly one of:
- `-t, --target TARGET`
- `--discover`

### ONVIF options

- `--skip-onvif`: skip ONVIF detection and enumeration
- `-oP, --onvif-port PORT`: test a specific ONVIF port
- `-ou, --onvif-username USER`: ONVIF username or file with one username per line
- `-op, --onvif-password PASS`: ONVIF password or file with one password per line
- `--reboot`: request a reboot via ONVIF and skip RTSP probing

### RTSP options

- `--skip-rtsp`: skip RTSP detection and brute-force
- `-P, --rtsp-port PORT`: test a specific RTSP port
- `-u, --username USER`: RTSP username or file with one username per line
- `-p, --password PASS`: RTSP password or file with one password per line
- `--protocol tcp|udp`: choose RTSP transport, default is `tcp`
- `--timeout SECONDS`: RTSP timeout, default is `10`
- `--vendor VENDOR`: force a vendor from the RTSP database
- `--record [OUTPUT.mp4]`: record the validated RTSP stream; if omitted, a timestamped file is created under `~/.pwneye/recordings`
- `--no-video`: skip live preview and decoding

### Misc options

- `--threads N`: number of concurrent threads
- `--no-cache`: do not read from or write to cache
- `--fresh`: ignore cache reads but still write new findings

## How Authentication Input Works

Both ONVIF and RTSP credential flags accept either:
- a literal value
- a file containing one value per line

Examples:

Use one username and rotate a password file:

```bash
python3 pwneye.py -t 192.168.1.135 --username admin --password ~/wordlists/passwords.txt
```

Use a username file and a fixed password:

```bash
python3 pwneye.py -t 192.168.1.135 --username ~/wordlists/users.txt --password admin123
```

Use one fixed credential pair only:

```bash
python3 pwneye.py -t 192.168.1.135 --username admin --password admin
```

Behavior summary:
- fixed `username + password`: only that exact pair is tested
- fixed `username` only: usernames stay fixed, passwords rotate
- fixed `password` only: passwords stay fixed, usernames rotate
- files on both sides: full cartesian product is tested

## Typical Assessment Flow

A practical workflow for a single target is:

1. Check ONVIF support and attempt ONVIF authentication
2. If ONVIF succeeds, extract device information, users, network settings, profiles, and stream URIs
3. Reuse the discovered ONVIF credential pair as the first RTSP credential candidate
4. Detect RTSP ports and attempt vendor-aware RTSP authentication
5. If needed, escalate to exhaustive RTSP path testing
6. Open the stream with `ffplay` or record it with `ffmpeg`

## Caching

pwneye stores runtime artifacts under `~/.pwneye`:
- `~/.pwneye/cache`: per-target cached ONVIF and RTSP successes
- `~/.pwneye/recordings`: default location for timestamped recordings

Cache behavior:
- by default, cached valid findings are reused before a fresh bruteforce
- `--fresh` ignores previously cached findings but still updates cache with new results
- `--no-cache` disables both cache reads and cache writes

## Recording

If a valid RTSP stream is found, you can:
- preview it with `ffplay`
- record it to MP4 with `--record`
- record it without preview with `--no-video --record`

Examples:

```bash
python3 pwneye.py -t 192.168.1.135 --record
python3 pwneye.py -t 192.168.1.135 --record living-room.mp4
python3 pwneye.py -t 192.168.1.135 --record living-room.mp4 --no-video
```

When `--record` is used without a filename, pwneye auto-generates a timestamped `.mp4` file in `~/.pwneye/recordings`.

## Safety

Use pwneye only against assets you own or are explicitly authorized to assess.

This tool can:
- enumerate authentication surfaces
- brute-force exposed services
- open and record live camera streams
- reboot ONVIF-capable devices

Treat it like any other offensive security tooling.

## TODO

- [ ] Add `--proxy` support to use a bridge machine that can reach cameras on its own local network
- [ ] Allow `--target` to accept a file with multiple IPs/hosts so scans can be parallelized across targets
- [ ] Add support for multi-channel cameras

## License

See `LICENSE`.
