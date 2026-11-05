# Connecting Stratix switches to Ghost Map (SNMP)

For Ghost Map v0.5 and later, on the machine HMI. Written for someone who is not a network specialist: every command is spelled out, and each step says how to tell it worked.

## What this is, in one paragraph

SNMP is how Ghost Map asks a managed switch questions: which ports are up, at what speed, what is plugged into each one, error counters, CPU and temperature. Ghost Map only **asks** (SNMP GET). It never changes the switch. But the switch will only answer someone who knows its SNMP password, so the one change you make is on the switch: you give it a **read-only** login for Ghost Map, and say which PCs may use it. Ghost Map then talks to the switch on **UDP port 161**.

Without SNMP, Ghost Map still finds every device, its firmware and faults. SNMP adds the **Switches** and **Traffic** pages and "which port is this device plugged into".

## What you need before you start

Write these down (the bracketed names are used in the steps below):

| | Example | Where to find it |
|---|---|---|
| Each switch's IP `[SWITCH-IP]` | `192.168.1.2` | Ghost Map **Network > Devices** or the machine drawings |
| The HMI's IP `[HMI-IP]` | `192.168.1.250` | On the HMI: Command Prompt, `ipconfig` |
| Your laptop's IP `[LAPTOP-IP]` (only if you'll scan from the laptop too) | `192.168.1.251` | `ipconfig` on the laptop |
| The switch's admin login | | Whoever commissioned the switch / controls engineer |
| A way to type commands on the switch | | SSH with PuTTY to `[SWITCH-IP]`, or a console cable |

**Make up the Ghost Map SNMP login now** and keep it in your password manager (not in email or the HMI notes):

- **SNMPv3 (recommended):** a user name `ghostmap`, an **auth key** and a separate **privacy key**. Each key **at least 8 characters** (the switch refuses shorter ones). Letters and numbers only keeps typing easy; avoid spaces, `?` and `"` (the switch's command line treats them specially).
- **SNMPv2c (fallback):** one **community string**, which is just a password. Never use `public` or `private`, at least 12 characters, letters and numbers.

> **Before touching a running machine:** this change does not affect PLC traffic, but it is still a switch config change. Do it with the controls engineer's OK, ideally when the machine is stopped.

## Step 1. Check the HMI can reach the switch

On the HMI, Command Prompt:

```bat
ping [SWITCH-IP]
```

You want `Reply from [SWITCH-IP]`. If it says `Request timed out` or `Destination host unreachable`, stop here: the HMI and switch aren't on the same network (check the HMI's IP and subnet mask with `ipconfig`). SNMP won't work until ping does.

You don't need to change Windows Firewall for this part. Ghost Map *sends* the SNMP question and Windows lets the reply back in by default.

## Step 2. Log in to the switch and look at what's there

Open PuTTY, Host Name `[SWITCH-IP]`, Connection type **SSH**, Open, and log in with the switch admin login. Then:

```
enable
show running-config | include snmp|access-list
```

(`enable` may ask for a second password.) Write down what it prints. Three things to look for:

- Nothing printed: SNMP isn't set up yet. Normal. Carry on.
- A line like `snmp-server community something RO`: someone already set up read-only SNMP. Leave it alone; we add our own.
- A line ending in `RW`: a **read-write** community already exists. Don't remove it during startup (something may use it), but tell the controls engineer; it's a security risk worth cleaning up later. **Never give Ghost Map an RW community.**

Also check the number `10` isn't already used in an `access-list 10` line. If it is, use `11` (or any unused number from 1 to 99) everywhere below instead of `10`.

## Step 3. Add the read-only login (pick ONE: 3a or 3b)

Type these one line at a time. Replace the bracketed parts. Lines starting with `!` are comments; you don't need to type them.

### 3a. SNMPv3 (recommended: the keys are never sent in the clear)

```
configure terminal
! who may ask: the HMI, plus your laptop if you'll scan from it
access-list 10 permit host [HMI-IP]
access-list 10 permit host [LAPTOP-IP]
! what they may read: everything, read-only
snmp-server view GHOSTMAP iso included
snmp-server group GHOSTMAP v3 priv read GHOSTMAP access 10
snmp-server group GHOSTMAP v3 priv context vlan- match prefix read GHOSTMAP access 10
! the login itself
snmp-server user ghostmap GHOSTMAP v3 auth sha [AUTH-KEY] priv aes 128 [PRIV-KEY] access 10
end
write memory
```

The `context vlan-` line lets Ghost Map read the MAC table of each VLAN; without it, the **Switches** page can't show which device is on which port.

Note: the switch never shows the keys again (`show running-config` won't list the `snmp-server user` line at all; that's normal). Check it with `show snmp user`.

### 3b. SNMPv2c (fallback, if v3 fights you)

```
configure terminal
access-list 10 permit host [HMI-IP]
access-list 10 permit host [LAPTOP-IP]
snmp-server community [COMMUNITY] RO 10
end
write memory
```

`RO` = read-only. `10` = only the PCs in access-list 10 may use it. Both matter.

### Don't forget `write memory`

Without it, the change is lost the next time the switch powers off. It should print `[OK]`.

Do Step 3 on **every** switch on the machine, with the same login, so one set of credentials in Ghost Map reads them all.

**Prefer the web page?** The Stratix web interface (Device Manager on 5400/5700, the WebUI on 5200/5800) has an SNMP page with the same settings (community or v3 user, plus the access list). Menu names vary by model and firmware; the commands above are the exact version and work on all of them over SSH.

## Step 4. Test from the HMI before touching the web UI

Ghost Map's command line can read one switch and print what it found. On the HMI, Command Prompt (in the folder with GhostMap.exe; for the installed service it's `C:\Program Files\GhostMap`):

**v3:**
```bat
GhostMap.exe switch [SWITCH-IP] --snmp-version 3 --v3-user ghostmap
```
It asks for the auth key and privacy key (nothing shows while you type; that's normal).

**v2c:**
```bat
set GHOSTMAP_COMMUNITY=[COMMUNITY]
GhostMap.exe switch [SWITCH-IP]
set GHOSTMAP_COMMUNITY=
```
(Using `set` keeps the community out of the command history. The last line clears it.)

**It worked** if the first line shows the switch's name and model, and you get a port table with links, speeds and devices. It takes about 15 seconds (it reads the counters twice, 10 seconds apart).

**It failed** if you see `! could not read system:` followed by an error. Look it up in the troubleshooting table at the end.

## Step 5. Put it into Ghost Map

There are two places, because they do different jobs:

### 5a. Network > Scan (the one-off map)

1. Open Ghost Map, **Network**, then **New scan** (admin login needed).
2. **Targets**: the machine subnet, e.g. `192.168.1.0/24`.
3. **Stratix / managed switch IPs**: optional. Leave **"Also read discovered CIP switches over SNMP"** ticked and Ghost Map finds the Stratix switches itself. List an IP here only for a switch that doesn't show up on its own (a non-Rockwell managed switch).
4. **SNMP**: pick v3 or v2c and type the same login as in Step 4.
5. **Start scan.** Then look at **Network > Switches**: you should see a port faceplate for each switch.

The scan dialog never saves the SNMP login: you type it for each scan.

### 5b. Network > Traffic (the 24/7 monitor)

This one keeps reading every 30 seconds, so it **does** save the login (sealed with Windows DPAPI, like the SQL password; it is never shown again).

1. **Network > Traffic**, **Settings** (admin).
2. **Switch IPs**: all the machine's switches, comma separated: `192.168.1.2, 192.168.1.3`.
3. Pick the SNMP version and enter the login. For v3: Auth `sha`, Privacy `aes` (= AES-128, matching Step 3a).
4. Press **Test** first. Each switch should show its name, model and port count. Then **Save and start**.

To change the login later, open Settings again: a blank key field means "keep the saved one".

## Step 6 (optional). Let the switch report problems to the HMI

The **Network > Live** page can show port down/up, err-disabled ports, loops and restarts the moment they happen, but only if the switch is told to send them to the HMI. On each switch:

```
configure terminal
logging host [HMI-IP]
logging trap warnings
snmp-server host [HMI-IP] version 2c [TRAP-COMMUNITY]
snmp-server enable traps snmp linkdown linkup coldstart warmstart authentication
end
write memory
```

`[TRAP-COMMUNITY]` is only a label on the messages; use a different word than your read community. Then on the HMI, allow them in (PowerShell, **Run as administrator**):

```powershell
New-NetFirewallRule -DisplayName "Ghost Map (syslog and traps)" -Direction Inbound -Protocol UDP -LocalPort 514,162 -Program "C:\Program Files\GhostMap\GhostMap.exe" -Action Allow
```

On the Live page, each listener says whether it is listening. If **SNMP traps** says the port is taken, another program on the HMI already has UDP 162 (usually Windows' own *SNMP Trap* service, or another trap receiver). Ghost Map won't fight it for the port. If nothing else needs it, stop and disable that service (`services.msc`, SNMP Trap), then restart Ghost Map.

## Troubleshooting

Find the error from Step 4 (or the **Test** button, or the `switch.collect` finding on the Scan page):

| What you see | What it means | What to do |
|---|---|---|
| `No SNMP response received before timeout` | The switch didn't answer at all. The switch stays **silent** for every one of these: wrong IP, SNMP not set up, wrong v2c community, or the HMI isn't in the access list. | 1) `ping [SWITCH-IP]`. 2) On the switch, `show running-config \| include snmp\|access-list`: is the HMI's IP in `access-list 10`, and is it the IP `ipconfig` shows? 3) Retype the community (it's case-sensitive). 4) Ask IT whether anything between HMI and switch blocks UDP 161 (rare on a machine network). |
| `Unknown USM user` | v3: the switch doesn't know that user name. | Check the spelling (`ghostmap`, case-sensitive). On the switch: `show snmp user`. |
| `Wrong SNMP PDU digest` | v3: user is right, **auth key** is wrong, or the auth type doesn't match. | Retype the auth key; Ghost Map's Auth must match the switch (`sha`). |
| `Ciphering services not available or ciphertext is broken` | v3: **privacy key** or type doesn't match. | Retype the privacy key; Privacy must be `aes` for `priv aes 128`. |
| Switch name shows, but no devices on the ports | The MAC tables (per VLAN) can't be read. | v3: add the `context vlan- match prefix` line from 3a. v2c: nothing extra is needed on IOS; check the finding's text. |
| A few health tiles missing (CPU, temperature, fans) | That switch model doesn't offer those readings. | Nothing to fix; it just shows fewer tiles. |
| v3 user worked yesterday, fails after a switch reboot | Rare; usually means `write memory` was skipped. | Redo Step 3 and `write memory`. |

For anything else, **System > Logs > Ghost Map log** and **Download debug bundle** (it contains IPs but never SNMP keys or communities).

## Security notes (sanitary, not Fort Knox)

- Read-only only: `RO` for v2c, a `read` view with no `write` view for v3. Ghost Map has no code to change a switch, but a read-only login means nobody else can use it to change one either.
- The access list is what keeps the login useful only from the HMI (and your laptop). If you stop scanning from the laptop, remove its line: `configure terminal`, `no access-list 10` then re-add only the HMI line, `end`, `write memory`.
- Ghost Map never writes SNMP credentials to its log, audit log, scan files or exports. Only the Traffic monitor stores them, sealed to that PC.
- Use a different SNMP login per site if you can, so one leaked community doesn't open every plant.
