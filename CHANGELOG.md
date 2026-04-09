# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
