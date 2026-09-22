# SpoolTap

## Roles

Ask-first repository. Workers never commit or push; they leave the working tree
changed and write a report for the maintainer to review before anything is
committed or pushed.

## What this is

SpoolTap is a HACS custom integration for Home Assistant (domain `spooltap`,
`integration_type: hub`, `iot_class: local_polling`) that adds phone-NFC
filament tracking on top of a Bambuddy instance. See README.md for the full
feature set and DEPLOY.md for install/upgrade instructions.

**This repo is the product face only.** All planning, scratch notes, and
development history live in a private companion repo — nothing here beyond the
public integration code, docs, and release history.

## Key facts

- Integration code lives under `custom_components/spooltap/`.
- Current release channel: GitHub Releases tagged `vX.Y.Z` for stable and
  `vX.Y.Zb<n>` for beta/pre-release (e.g. `v0.3.3` stable, `v0.4.0b1`
  pre-release) — HACS follows these tags.
- `hacs.json` pins the minimum supported Home Assistant version; `manifest.json`
  carries the integration's own version, which must match the release tag.
- CI (`.github/workflows/`) runs Home Assistant's `hassfest` and HACS repository
  validation on every push and pull request.

## Guardrails

- No personal data, planning documents, or internal development notes are ever
  added to this repo — that content belongs in the private companion repo.
- Codeowner: `@dmuth23`.
