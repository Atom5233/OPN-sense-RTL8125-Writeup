# OPNsense LAN dropouts under sustained load: RTL8125 TX hang

A write-up of a long troubleshooting session on a home OPNsense router. The LAN would go completely dark under heavy, sustained traffic. The router stayed alive at the console and its own WAN connection kept working. Most of the obvious suspects turned out to be wrong. The real cause was the Realtek RTL8125 2.5GbE NIC's transmit queue wedging, and the fix was moving the LAN to an Intel I226-V.

If you're seeing "LAN dies, console still works, router can still ping the internet, needs a reboot to recover" on a Realtek 2.5G NIC, this might save you some time.

**In this repo**

| Path | |
|---|---|
| [`scripts/watch.sh`](scripts/watch.sh) | Logs `ifconfig` and `netstat -i` every 15 s to a file that survives reboots |
| [`scripts/analyze.py`](scripts/analyze.py) | Finds when `DRV_OACTIVE` stuck on, shows rates around it, exports a CSV |
| [`logs/failure-window.log`](logs/failure-window.log) | Sanitised raw samples either side of the failure |
| [`logs/samples.csv`](logs/samples.csv) | The whole night, one row per sample |

---

## TL;DR

| | |
|---|---|
| **Symptom** | LAN clients lose all access (can't ping or reach the router GUI or SSH). Router console is fine and the router can still ping 8.8.8.8. Only a reboot recovers it. |
| **Trigger** | Sustained high packet rate, in this case a containerised workload on a TrueNAS server moving ~10–17k packets/sec through the router for hours. |
| **Root cause** | `re(4)` driver / RTL8125 transmit ring wedges. `DRV_OACTIVE` gets stuck on and never clears. Zero errors, link stays `active`. |
| **Fix** | Move LAN to an Intel I226-V (`igc`), with `dev.igc.0.eee_control=0` and `dev.igc.0.fc=0`. |
| **Result** | Realtek failed after ~2h38m. The Intel ran far longer under heavier load (67M+ packets) with zero errors and zero link flaps. |

---

## Setup

**Router**
- Repurposed Dell laptop motherboard, Intel i5-8365U, 8 GB RAM, SATA SSD (ZFS root)
- OPNsense 26.7.2_2 on FreeBSD 15.1
- Two M.2 NICs:
  - Realtek RTL8125 2.5GbE (`re` driver)
  - Intel I226-V 2.5GbE (`igc` driver), NVM firmware 2.17
- WAN: BT FTTP, PPPoE, roughly 1.5 Gbps down / 60 Mbps up

**LAN**
- TrueNAS SCALE server, single NIC on a bridge (`br0`)
- A Docker container running a high-throughput workload: sustained inbound transfers from the internet, for hours at a time

---

## Symptoms

- Internet "drops" for every LAN device
- LAN clients **cannot ping the router's LAN IP**, so it's layer 2, not DNS or routing
- WebGUI and SSH unreachable
- **Local console stays fully responsive**
- From the console, the router **can still ping 8.8.8.8**, the PPPoE session stays up, and the default route stays present
- Doesn't recover on its own. A reboot fixes it, until the next time.

The key isolation test was a physical one: **unplug the TrueNAS box and the problem stops. Plug it back in, and it comes back.** Stopping only that one Docker container for several days kept things stable, which narrowed it to that container's traffic.

---

## What it *wasn't* (and what I tried)

A lot of time went into theories that didn't hold up. Listed here so you can skip them.

| Theory | What was done | Outcome |
|---|---|---|
| CPU power management | PowerD `minimum` / `hiadaptive` / `maximum` | No effect |
| Deep C-states | `hw.acpi.cpu.cx_lowest=C3` | Reverted. Not the cause. |
| Intel I226 EEE link-flap bug (on the *original* WAN port) | Tunables, attempted NVM flash | Real bug, but separate from this problem (see below) |
| PPPoE / DHCPv6 reconfiguration loop | IPv6 on WAN set to None | Still dropped |
| Hardware offloads (TSO/LRO/checksum) | Disabled | Correct practice for a router, but still dropped |
| Firewall state table exhaustion | Max states raised to 2M | Peak was only ~1.4k states. Not the cause. |
| Connection volume from the workload | Its connection and concurrency limits cut hard | Still dropped |
| `privileged: true` on the container | Removed, granted only the capabilities it needed | Good hygiene, but still dropped |
| Accumulated config cruft | Factory reset of OPNsense | Still dropped |
| ARP conflict / broadcast storm | `arp -a`, error counters | Clean. No conflicts, no errors. |

---

## How it was actually found

Single snapshots kept looking healthy. What cracked it was logging interface state every 15 seconds overnight, so the moment of failure was captured.

The logger is [`scripts/watch.sh`](scripts/watch.sh). OPNsense's root shell is `csh`, so run it with `sh`:

```sh
sh /root/watch.sh re0 /root/watch.log &
```

`/root` survives reboots, so you can read it after recovery. Copy it off with `scp root@<router>:/root/watch.log .` and summarise it with [`scripts/analyze.py`](scripts/analyze.py):

```sh
python3 scripts/analyze.py watch.log --iface re0 --csv samples.csv
```

### The evidence

Output of `analyze.py` on the overnight log:

```
Samples: 2288  span: 9:32:29  gaps >25s: 0
re0: DRV_OACTIVE stuck on from 2026-08-27 23:34:54 UTC
  before: 0/631 samples set (0%)
  after:  1656/1656 samples set (100%)
  around the transition:
    2026-08-27 23:34:09  WAN in   10355 pps  OACTIVE=no
    2026-08-27 23:34:24  WAN in   11527 pps  OACTIVE=no
    2026-08-27 23:34:39  WAN in   10931 pps  OACTIVE=no
    2026-08-27 23:34:54  WAN in    5294 pps  OACTIVE=yes   <-- wedge
    2026-08-27 23:35:09  WAN in      36 pps  OACTIVE=yes
    2026-08-27 23:35:24  WAN in      21 pps  OACTIVE=yes
    2026-08-27 23:35:39  WAN in      24 pps  OACTIVE=yes
Max cumulative error count on any interface: 0
```

- The log has **no gaps** over 9.5 hours, so the router never crashed or rebooted.
- WAN traffic ran at ~10–17k packets/sec for about 2h38m, then fell to near zero in one sample and **stayed there for seven hours**.
- `DRV_OACTIVE` on the LAN NIC was clear in **every one** of the 631 samples while it was working, and set in **every one** of the 1,656 samples after it died.

`DRV_OACTIVE` means "the driver's transmit queue is busy." It can appear briefly during normal transmits, but on an interface that's almost completely idle it should be clear. Being stuck on for seven hours while nothing was being sent means the TX ring had jammed.

The raw samples either side of the wedge are in [`logs/failure-window.log`](logs/failure-window.log), and the whole night as one row per sample is in [`logs/samples.csv`](logs/samples.csv).

Throughout, the following stayed normal:
- `status: active`, media unchanged, link never dropped
- Zero `Ierrs` / `Idrop` / `Oerrs` / `Coll` on both NICs
- PPPoE session never dropped
- Offloads stayed disabled (`options=2028<VLAN_MTU,JUMBO_MTU,WOL_MAGIC>`)

That matches the symptoms exactly. The router can't transmit to the LAN, so clients get silence. The console is unaffected because it isn't networking. The WAN looks fine from the router because the WAN side isn't the one that's stuck.

> Caveat: a single snapshot showing `DRV_OACTIVE` proves nothing on its own. It took the long log, with the flag clear for the whole working period and stuck on for the whole dead period, to make it conclusive.
>
> Note that the `re` driver reports `Opkts 0` on its `<Link#>` row in `netstat -i`, so you can't watch its transmit counter directly. The Intel `igc` driver does report it, which makes a stall much easier to spot.

---

## The fix

Both NICs were M.2 cards, so the swap was physical. After swapping, OPNsense re-enumerated the devices (the old `re1` became `re0`), so interfaces had to be reassigned from the console.

1. Back up the config: **System → Configuration → Backups**
2. `shutdown -p now`, swap the cards
3. At the console: option **1** (Assign interfaces). LAN → `igc0`, WAN → the Realtek port.
4. Option **2** to set the LAN IP back to what it was
5. Add tunables (**System → Settings → Tunables**), then reboot:

   ```
   dev.igc.0.eee_control = 0
   dev.igc.0.fc = 0
   ```

6. Confirm the offload settings carried over: **Interfaces → Settings**, tick all three "Disable hardware … offload" boxes, set VLAN Hardware Filtering to Disable, **Apply**, reboot. Check with `ifconfig igc0`.

Final layout:
- **LAN**: Intel I226-V (`igc0`)
- **WAN**: Realtek RTL8125 (`re0`) carrying PPPoE

The WAN side mostly *receives*, so it puts much less load on the Realtek's transmit path. It handled 67M+ packets without issue during testing.

### Result

| NIC on LAN | Time to failure | Notes |
|---|---|---|
| Realtek RTL8125 (`re`) | ~2h38m | TX queue wedged permanently |
| Intel I226-V (`igc`) | No failure | 65.9M packets transmitted, 0 errors, 0 link flaps, with the workload's limits raised well above where the Realtek failed |

Track I226 link flaps with:

```sh
grep -c 'igc0: link state changed to DOWN' /var/log/system/latest.log
```

It sat at 4 for the whole test, and all four were boot-time interface initialisation.

---

## Side quest: I226-V firmware

The I226-V has a well-known EEE bug where the link drops and renegotiates under light traffic. Intel fixed it in NVM 2.22+. This card was on **2.17** (`eTrack 0x80000308`).

I tried flashing it from OPNsense with `nvmupdate64e`, using [advallespir/intel-i226v-fix-bsd](https://github.com/advallespir/intel-i226v-fix-bsd):

- Recent tool builds refuse to write on platforms without the Microsoft UEFI CA 2023 certificate, even with Secure Boot off. The tool's own help lists `-AllowMissingCert` to bypass this. Note it conflicts with `-i` (inventory mode), so only use it on the update.
- After that, the write was rejected with **`id=6` "An error occurred when updating a firmware module"** with both the 2MB and the `-if vendor` path. The 1MB config didn't recognise the device at all (`id=8`).
- Every failure was clean: NIC untouched, checksum valid, backup created.

On an OEM laptop board, this looks like a locked NVM that the tool can't write past. In the end it didn't matter. **With `eee_control=0` the card never flapped, even on 2.17.**

If you're buying a retail M.2 I226 card today, it will likely ship with newer firmware anyway.

---

## lessons

- **OPNsense's root shell is csh.** Bash-style loops and `$(...)` fail. Run `sh` first, and remember background jobs die when that shell exits (use `nohup` if needed).
- **GUI tunable type matters.** Some tunables (e.g. `hw.acpi.cpu.cx_lowest`) must be loader-stage. If it never appears in `/boot/loader.conf.d/`, it isn't being applied. `/boot/loader.conf.local` is a reliable fallback.
- **Read what a tunable does before copying it.** `hw.pci.do_power_nodriver=3` is sometimes posted as an I226 fix. Per `sysctl -d`, `3` means "put absolutely everything in D3 state," the most aggressive power-down setting. `hw.igc.disable_aspm` doesn't exist on FreeBSD 15.1's `igc` driver at all ("unknown oid").
- **Change one thing at a time.** Several changes at once made earlier results impossible to interpret.
- **"Use Intel, avoid Realtek" was correct here, but for a narrow reason.** Intel's I225/I226 line has its own bugs. What mattered was that the specific `re` TX path couldn't sustain this workload.
- **The netisr multi-thread PPPoE tunables weren't needed.** `top` showed ~90% idle on the i5-8365U at full load. Check `top -P` for a single pinned core before adding them.
- **After a factory reset, re-check offload settings.** The reset silently turned TSO/LRO back on.
- **BT PPPoE username** is `bthomehub@btbroadband.com` (password can be anything non-empty). A typo here just looks like "no connection."

---

## Useful commands

```sh
# Interface state, flags, offloads
ifconfig igc0
ifconfig re0

# Packet and error counters (compare two runs to get a rate)
netstat -i

# Firewall state table
pfctl -si

# ARP table (look for duplicate IPs / MACs)
arp -a

# Link flaps
grep -i 'link state' /var/log/system/latest.log | tail -20

# Per-core CPU (look for one pinned core = single-threaded bottleneck)
top -P

# NIC firmware version
dmesg | grep -i 'igc0.*EEPROM'
```

---

*Hardware: i5-8365U / OPNsense 26.7.2_2 / FreeBSD 15.1 / Realtek RTL8125 and Intel I226-V (M.2). Your results may differ on other chip revisions and driver versions.*
