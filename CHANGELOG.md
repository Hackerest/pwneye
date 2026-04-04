# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

### Changed

### Removed

## [1.0.0] - 2026-04-05 (Panopticon)

### Added

- ONVIF discovery mode via `--discover` for local-network WS-Discovery enumeration
- Multithreaded ONVIF authentication with live progress output showing ports and credentials being tested
- ONVIF service probing before bruteforce to reduce wasted requests on non-ONVIF ports
- ONVIF post-auth enumeration for device information, configured users, network configuration, media profiles, and RTSP stream URIs
- ONVIF reboot support via `--reboot`
- Multithreaded RTSP bruteforce with live spinner output and per-attempt connection visibility
- RTSP vendor-aware bruteforce using the built-in RTSP knowledge base
- Exhaustive RTSP path fallback using the full path database when vendor identification fails or vendor-specific paths do not work
- Per-target cache support for successful ONVIF and RTSP findings under `~/.pwneye/cache`
- Automatic RTSP preview support via `ffplay`
- RTSP recording support via `ffmpeg`, including timestamped default filenames under `~/.pwneye/recordings`
- Credential file support for ONVIF and RTSP username/password arguments

### Changed

- Promoted the release branding to `v1.0_panopticon`
- Updated the CLI help so target selection is expressed as `--target` or `--discover`, instead of implying a single strict target-only mode
- Refined RTSP bruteforce ordering so discovered ONVIF credentials are tried first across RTSP paths and ports
- Improved RTSP port discovery to prioritize common ports first and use less noisy live output
- Improved exhaustive RTSP prompting with clearer warnings and explicit request-volume feedback
- Improved `CTRL-C` handling across ONVIF, RTSP, preview, and recording flows to reduce stack traces and inconsistent shutdown behavior
- Improved recording behavior so `--record` can auto-generate a destination filename when none is provided
- Improved user feedback around unstable devices after aggressive RTSP bruteforce and around recovery through `--reboot` when ONVIF access was confirmed
- Updated the banner credit to `Coded by robo7nik`
- Reworked the project README to provide fuller installation, usage, and workflow guidance for camera assessments

### Removed

- Removed the old importer script and the temporary `scripts/` directory used during RTSP database population
- Removed ONVIF system log extraction from the default enumeration flow to avoid unnecessarily invasive post-auth behavior
- Removed outdated `0.1.0` placeholder release metadata in favor of the first public `1.0.0` release entry
