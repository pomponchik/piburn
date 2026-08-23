<details>
  <summary>ⓘ</summary>

[![Downloads](https://static.pepy.tech/badge/piburn/month)](https://pepy.tech/project/piburn)
[![Downloads](https://static.pepy.tech/badge/piburn)](https://pepy.tech/project/piburn)
[![Coverage Status](https://coveralls.io/repos/github/pomponchik/piburn/badge.svg?branch=main)](https://coveralls.io/github/pomponchik/piburn?branch=main)
[![Lines of code](https://sloc.xyz/github/pomponchik/piburn/?category=code)](https://github.com/boyter/scc/)
[![Hits-of-Code](https://hitsofcode.com/github/pomponchik/piburn?branch=main)](https://hitsofcode.com/github/pomponchik/piburn/view?branch=main)
[![Tests](https://github.com/pomponchik/piburn/actions/workflows/tests_and_coverage.yml/badge.svg)](https://github.com/pomponchik/piburn/actions/workflows/tests_and_coverage.yml)
[![Lint](https://github.com/pomponchik/piburn/actions/workflows/lint.yml/badge.svg)](https://github.com/pomponchik/piburn/actions/workflows/lint.yml)
[![Python versions](https://img.shields.io/pypi/pyversions/piburn.svg)](https://pypi.org/project/piburn/)
[![PyPI version](https://badge.fury.io/py/piburn.svg)](https://badge.fury.io/py/piburn)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/pomponchik/piburn)

</details>

![logo](https://raw.githubusercontent.com/pomponchik/piburn/develop/docs/assets/logo_1.svg)


`piburn` prepares removable microSD cards that boot Raspberry Pis with Ubuntu Server for use as cluster nodes. By default, it downloads the latest stable [Ubuntu Server image for Raspberry Pi](https://ubuntu.com/download/raspberry-pi) and verifies its published SHA-256 checksum. It writes settings that Ubuntu applies on first boot through [cloud-init](https://docs.cloud-init.io/en/latest/), safely ejects each card, and can generate an Ansible inventory (a list of nodes to manage).

The tool is intentionally conservative: it only lets you select physical, writable, removable media of at least 4 GiB and rechecks the selected device's identifying attributes before erasing or writing to it.


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

Install it from [PyPI](https://pypi.org/project/piburn/):

```bash
pip install piburn
```

The `piburn` command is now available:

```bash
piburn --help
```


## Quick start

> **Warning:** flashing and integrity testing can destroy existing data on the selected card. Integrity testing overwrites its entire reported capacity. Always verify the device name, model, and capacity before confirming it.

Insert a microSD card and run:

```bash
piburn
```

The interactive interface first asks how many cards to prepare and whether to run the full integrity test, then collects the Wi-Fi, hostname, and login settings. For each card, it asks you to select the target device. The starting hostname number defaults to `1`. Cards are prepared one at a time, so a single card reader is enough.

Press `Ctrl+C` at any step to stop.

After the last card, the tool asks whether to generate an [Ansible inventory](https://docs.ansible.com/projects/ansible/latest/inventory_guide/intro_inventory.html). If you accept, it creates or replaces `ansible/inventory.ini` by default; this file lists the nodes managed by Ansible. It then prints one SSH command per node:

```text
SSH commands:
ssh pomponchik@pi-1.local
ssh pomponchik@pi-2.local
```

Insert each ejected card into a Raspberry Pi and power it on. Use the corresponding command after the first boot completes.


## What gets configured

Every card receives:

- the selected OS image;
- a numbered hostname such as `pi-1`;
- the `pomponchik` user by default, configurable with `--username`;
- Wi-Fi and Ethernet configured to obtain network settings automatically;
- either an SSH public key or a shared login password;
- [Avahi](https://avahi.org/) for discovery of `.local` names such as `pi-1.local`;
- cloud-init configuration that expands Ubuntu to use the card's full capacity on first boot.

Downloaded images are temporary; local files supplied through `--image` are left untouched.


## Login methods

For SSH-key login, `piburn` looks for an existing SSH public key in common `~/.ssh` locations, starting with `~/.ssh/id_ed25519.pub`. Create one when needed:

```bash
ssh-keygen -t ed25519
```

Alternatively, choose password login. `piburn` displays a random password; press Enter to accept it or type your own, and save whichever password you use. Only its hash is written to the card.

In interactive mode, the Wi-Fi password is hidden while you type, and `piburn` does not save a persistent copy on the Mac. It must still be written to the card so the Raspberry Pi can join the network.


## Card integrity test

The optional full integrity test writes a newly randomized, position-dependent pattern across the card's entire reported capacity and reads it back without using the local read cache. This can detect corrupted or counterfeit media, including stale data from an earlier test, but it is destructive and may take longer than flashing Ubuntu itself.

After Ubuntu is written, `piburn` performs a separate byte-for-byte comparison between the card and a freshly verified and decompressed source image. The full-card test and this post-flash verification cover different write operations: passing the first does not guarantee that a later image write cannot fail. Writes request an explicit `fsync`, and verification reads bypass the macOS cache. If data differs, `piburn` reports the first differing byte, full and per-block SHA-256 values, and whether repeated direct reads are stable, transient, or inconsistent. Any inconsistent read remains an error.

Downloaded images are normally removed after the run, including after automatic mismatch diagnostics. To retain the downloaded image and a secret-free diagnostic report after a failure, pass a destination directory:

```bash
piburn --keep-image-on-failure ./piburn-diagnostics
```

For a remote image, `piburn` stages the temporary download on the destination filesystem so it can be preserved by an atomic rename without creating another multi-gigabyte copy. A successful run removes that staging directory without creating a failure subdirectory. After a failed or cancelled run, `piburn` creates a unique subdirectory and prints the resulting paths. A local image is not duplicated; the report refers to its existing path. Failure to preserve these artifacts is reported separately and never replaces the original write or verification error.


## Non-interactive usage

`--non-interactive` disables `piburn`'s own prompts, but `sudo` may still request administrator authentication once at the start. `piburn` keeps that authorization active during long writes and checks; macOS may ask again only if the authorization is revoked or the system uses an unusually short timeout. Passwords are read from environment variables rather than command-line arguments. Replace `wifi-password`, `MyNetwork`, and the sample device paths with your own values; `--yes` authorizes writing to those devices without confirmation.

```bash
export PIBURN_WIFI_PASSWORD='wifi-password'

piburn \
  --non-interactive \
  --count 2 \
  --no-check \
  --ssid 'MyNetwork' \
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
