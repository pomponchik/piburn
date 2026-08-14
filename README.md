<details>
  <summary>ⓘ</summary>

[![Tests](https://github.com/pomponchik/piburn/actions/workflows/tests_and_coverage.yml/badge.svg)](https://github.com/pomponchik/piburn/actions/workflows/tests_and_coverage.yml)
[![Lint](https://github.com/pomponchik/piburn/actions/workflows/lint.yml/badge.svg)](https://github.com/pomponchik/piburn/actions/workflows/lint.yml)
[![Python versions](https://img.shields.io/pypi/pyversions/piburn.svg)](https://pypi.org/project/piburn/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

</details>

![logo](https://raw.githubusercontent.com/pomponchik/piburn/develop/docs/assets/logo_1.svg)


`piburn` turns removable microSD cards into ready-to-boot Ubuntu Server nodes for a Raspberry Pi cluster. It downloads and verifies the latest stable Raspberry Pi image, writes cloud-init configuration, safely ejects each card, and optionally generates an Ansible inventory.

The tool is intentionally conservative: it only offers physical, writable, removable media of at least 4 GiB and verifies that the selected device has not changed before destructive operations.


## Table of Contents

- [**Installation**](#installation)
- [**Quick start**](#quick-start)
- [**What gets configured**](#what-gets-configured)
- [**Login methods**](#login-methods)
- [**Card integrity test**](#card-integrity-test)
- [**Non-interactive usage**](#non-interactive-usage)


## Installation

`piburn` requires macOS and Python 3.8 or newer. It has no third-party runtime dependencies.

Install it:

```bash
pip install piburn
```

The `piburn` command is now available:

```bash
piburn --help
```


## Quick start

Insert a microSD card and run:

```bash
piburn
```

The interactive interface asks for the number of cards, Wi-Fi credentials, hostname prefix, starting hostname number, login method, and target device. The starting number defaults to `1`. Cards are prepared one at a time, so a single card reader is enough.

At the end, the tool can replace `ansible/inventory.ini` and prints one ready-to-use command per node:

```text
SSH commands:
ssh pomponchik@pi-1.local
ssh pomponchik@pi-2.local
```

Press `Ctrl+C` at any step to stop.

> **Warning:** flashing and integrity testing erase the selected card completely. Always verify the device name, model, and capacity before confirming it.


## What gets configured

Every card receives:

- the latest published stable Ubuntu Server ARM64 image for Raspberry Pi;
- a unique hostname such as `pi-1.local`;
- the `pomponchik` user by default;
- Wi-Fi with DHCP and optional Ethernet DHCP for a future wired switch;
- either an SSH public key or a shared login password;
- Avahi for `.local` name discovery;
- cloud-init configuration that expands the root filesystem on first boot.

Remote images are downloaded into a temporary directory and removed after success, failure, or `Ctrl+C`. They are never kept in a persistent cache. A local file supplied through `--image` is never deleted.


## Login methods

For passwordless access, `piburn` discovers a standard public key such as `~/.ssh/id_ed25519.pub`. Create one when needed:

```bash
ssh-keygen -t ed25519
```

Alternatively, choose password login. The generated default is a random 20-character alphanumeric password, and only its SHA-512 crypt hash is written to cloud-init.

The Wi-Fi password is hidden while typing and is not stored on the Mac after the process exits. It must be written to the card's `network-config` so the Raspberry Pi can join the network.


## Card integrity test

The optional full integrity test writes a deterministic pattern across the card's entire reported capacity and reads it back. This can detect corrupted or counterfeit media, but it is destructive and may take longer than flashing Ubuntu itself.


## Non-interactive usage

Passwords are accepted through environment variables rather than command-line arguments:

```bash
export PIBURN_WIFI_PASSWORD='wifi-password'

piburn \
  --non-interactive \
  --count 2 \
  --no-check \
  --ssid MyNetwork \
  --wifi-password-env PIBURN_WIFI_PASSWORD \
  --prefix pi \
  --start-number 1 \
  --auth-mode ssh-key \
  --device /dev/disk4 \
  --device /dev/disk5 \
  --inventory \
  --inventory-path ansible/inventory.ini \
  --yes

unset PIBURN_WIFI_PASSWORD
```

To reuse one card reader, repeat the same `--device` value. After ejecting a completed card, `piburn` waits for the next one.

For password login, also export a user password:

```bash
export PIBURN_USER_PASSWORD='node-password'
```

Then replace `--auth-mode ssh-key` in the command with
`--auth-mode password --user-password-env PIBURN_USER_PASSWORD`. Unset both
password variables after the run.

Run `piburn --help` for every available option.
