# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

## [1.1.0] - 2026-05-03 (Panopticon)

### Added

- RTSP snapshot support via `--snapshot`, including timestamped default filenames and integration with the existing preview / no-video flows
- Manual RTSP connection string support via `--connection-string` / `-cn`, with file input support and compatibility with templated multi-channel paths such as `{channel}`
- First public iteration of RTSP multi-channel support, including:
  - `--multi-channel` path prioritization
  - channel-aware RTSP template expansion
  - interactive channel selection
  - per-target caching of discovered channels
- Runtime update checks against the latest GitHub release, plus an explicit `--check-updates` mode
- MOTD support with randomized startup messages loaded from the local knowledge base

### Changed

- Automatic recording and snapshot outputs are now stored under per-target directories inside `~/.pwneye/recordings` and `~/.pwneye/snapshots`
- RTSP target resolution now supports user-specified connection strings as first-class inputs, reducing noise when the stream path is already known
- Cache handling is more explicit when credentials are passed on the command line, avoiding misleading reuse messages and clarifying when cached results are ignored
- ONVIF reboot feedback is now more descriptive and performs an automatic reachability check after the reboot request is sent
- General RTSP and ONVIF flow messaging has been refined to reduce redundancy and make scanning state easier to understand
- `--help` output has been revised to make fallback behaviors, optional inputs, and RTSP targeting options clearer

### Fixed

- Improved `CTRL-C` handling across interactive prompts, scanning loops, recording, and channel enumeration to reduce stack traces and inconsistent exits
- Improved post-discovery RTSP handling so preview, snapshot, recording, and multi-channel flows behave more consistently across flag combinations
- Improved cache interactions for targets that were already known, especially when the user forces fresh scans or supplies explicit credentials

## [1.0.0] - 2026-04-05 (Panopticon)

### Added

- First public release of `pwneye`
- ONVIF local-network discovery via `--discover`, with continuous probing and live output for newly discovered devices
- ONVIF service probing before bruteforce to reduce wasted requests on non-ONVIF ports
- Multithreaded ONVIF authentication with live progress output showing ports and credentials being tested
- ONVIF support for single credentials or username/password wordlists passed directly on the command line
- ONVIF post-auth enumeration for device information, configured users, network configuration, media profiles, and RTSP stream URIs
- ONVIF reboot support via `--reboot`
- RTSP port discovery with prioritization of the most common RTSP ports first
- RTSP banner retrieval and banner-based vendor identification
- RTSP vendor listing via `--list-vendors`
- Vendor-aware RTSP bruteforce using the built-in RTSP knowledge base
- Exhaustive RTSP fallback using the full path database when vendor identification fails or vendor-specific paths do not work
- Multithreaded RTSP bruteforce with live spinner output and per-attempt connection visibility
- RTSP preview support via `ffplay`
- RTSP recording support via `ffmpeg`, including timestamped default filenames under `~/.pwneye/recordings`
- Dedicated RTSP banner mode via `--banner`
- Per-target cache support for successful ONVIF and RTSP findings under `~/.pwneye/cache`
- Caching of RTSP banners and ONVIF-discovered manufacturer hints to improve later targeting
- Credential file support for ONVIF and RTSP username/password arguments
- Improved interrupt handling across ONVIF, RTSP, preview, recording, and interactive prompts
- Project branding for the `v1.0.0_panopticon` release line
