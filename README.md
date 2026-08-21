<details>
  <summary>ⓘ</summary>

[![Tests](https://github.com/pomponchik/piburn/actions/workflows/tests_and_coverage.yml/badge.svg)](https://github.com/pomponchik/piburn/actions/workflows/tests_and_coverage.yml)
[![Lint](https://github.com/pomponchik/piburn/actions/workflows/lint.yml/badge.svg)](https://github.com/pomponchik/piburn/actions/workflows/lint.yml)
[![Python versions](https://img.shields.io/pypi/pyversions/piburn.svg)](https://pypi.org/project/piburn/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

</details>

![logo](https://raw.githubusercontent.com/pomponchik/piburn/develop/docs/assets/logo_1.svg)


`piburn` prepares removable microSD cards that boot Raspberry Pis as Ubuntu Server cluster nodes. It downloads and verifies the latest stable Ubuntu Server image for Raspberry Pi, writes settings that Ubuntu applies on first boot through cloud-init, safely ejects each card, and can generate an Ansible inventory (a list of nodes to manage).

The tool is intentionally conservative: it only lets you select physical, writable, removable media of at least 4 GiB and verifies that the selected device has not changed before erasing or writing to it.


## Table of Contents

- [**Installation**](#installation)
- [**Quick start**](#quick-start)
- [**What gets configured**](#what-gets-configured)
- [**Login methods**](#login-methods)
- [**Card integrity test**](#card-integrity-test)
- [**Non-interactive usage**](#non-interactive-usage)


## Installation

`piburn` requires macOS and Python 3.8 or newer. It has no third-party runtime dependencies.
Writing to a card requires macOS administrator privileges.

Install it:

```bash
pip install piburn
```

The `piburn` command is now available:

```bash
piburn --help
```


## Quick start

> **Warning:** flashing and integrity testing erase the selected card completely. Always verify the device name, model, and capacity before confirming it.

Insert a microSD card and run:

```bash
piburn
```

The interactive interface first asks how many cards to prepare and whether to run the full integrity test, then collects the Wi-Fi, hostname, and login settings. For each card, it asks you to select the target device. The starting hostname number defaults to `1`. Cards are prepared one at a time, so a single card reader is enough.

Press `Ctrl+C` at any step to stop.

After the last card, the tool asks whether to generate an Ansible inventory. If you accept, it creates or replaces `ansible/inventory.ini` by default; this file lists the nodes managed by Ansible. It then prints one SSH command per node:

```text
SSH commands:
ssh pomponchik@pi-1.local
ssh pomponchik@pi-2.local
```

Insert each ejected card into a Raspberry Pi and power it on. Use the corresponding command after the first boot completes.


## What gets configured

Every card receives:

- an OS image—by default, the latest published stable Ubuntu Server ARM64 release for Raspberry Pi;
- a unique hostname such as `pi-1`, reachable on the local network as `pi-1.local`;
- the `pomponchik` user by default, configurable with `--username`;
- Wi-Fi and Ethernet configured to obtain network settings automatically, without delaying boot when no Ethernet cable is connected;
- either an SSH public key or a shared login password;
- Avahi for `.local` name discovery;
- cloud-init configuration that expands Ubuntu to use the card's full capacity on first boot.

Remote images are downloaded into a temporary directory and removed after success, failure, or `Ctrl+C`. They are never kept in a persistent cache. A local file supplied through `--image` is never deleted.


## Login methods

For SSH-key login, `piburn` looks for an existing SSH public key in common `~/.ssh` locations, starting with `~/.ssh/id_ed25519.pub`. Create one when needed:

```bash
ssh-keygen -t ed25519
```

Alternatively, choose password login. `piburn` displays a random 20-character alphanumeric password; press Enter to accept it or type your own, and save whichever password you use. Only its SHA-512 crypt hash, not the plaintext password, is written to the card's cloud-init configuration.

In interactive mode, the Wi-Fi password is hidden while you type, and `piburn` does not save a persistent copy on the Mac. It is still written to the card's `network-config` so the Raspberry Pi can join the network.


## Card integrity test

The optional full integrity test writes test data across the card's entire reported capacity and reads it back. This can detect corrupted or counterfeit media, but it is destructive and may take longer than flashing Ubuntu itself.


## Non-interactive usage

Passwords are read from environment variables rather than command-line arguments. Replace `wifi-password`, `MyNetwork`, and the sample device paths with your own values; `--yes` explicitly authorizes erasing those devices.

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

This example uses `--no-check`, so it skips both the full card integrity test and post-flash read-back verification.

To reuse one card reader, repeat the same `--device` value. After ejecting a completed card, `piburn` waits for the next one.

To adapt this example for password login, add this export before the `piburn` command:

```bash
export PIBURN_USER_PASSWORD='node-password'
```

Also replace `--auth-mode ssh-key` in the command with
`--auth-mode password --user-password-env PIBURN_USER_PASSWORD`. Unset both
password variables after the run.

Run `piburn --help` to see all available options.
