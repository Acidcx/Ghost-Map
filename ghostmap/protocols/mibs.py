"""OIDs read from Stratix (Cisco IOS based) switches. All of them are read-only GET/WALK."""

# SNMPv2-MIB system group (scalars)
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"
SYSTEM_SCALARS = (SYS_DESCR, SYS_OBJECT_ID, SYS_UPTIME, SYS_CONTACT, SYS_NAME, SYS_LOCATION)

# IF-MIB ifTable / ifXTable (index: ifIndex)
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_TYPE = "1.3.6.1.2.1.2.2.1.3"
IF_SPEED = "1.3.6.1.2.1.2.2.1.5"
IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"
IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
IF_LAST_CHANGE = "1.3.6.1.2.1.2.2.1.9"
IF_IN_DISCARDS = "1.3.6.1.2.1.2.2.1.13"
IF_IN_ERRORS = "1.3.6.1.2.1.2.2.1.14"
IF_OUT_DISCARDS = "1.3.6.1.2.1.2.2.1.19"
IF_OUT_ERRORS = "1.3.6.1.2.1.2.2.1.20"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_HIGH_SPEED = "1.3.6.1.2.1.31.1.1.1.15"
IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"

# EtherLike-MIB dot3StatsTable (index: ifIndex)
DOT3_ALIGNMENT_ERRORS = "1.3.6.1.2.1.10.7.2.1.2"
DOT3_FCS_ERRORS = "1.3.6.1.2.1.10.7.2.1.3"
DOT3_LATE_COLLISIONS = "1.3.6.1.2.1.10.7.2.1.8"
DOT3_DUPLEX_STATUS = "1.3.6.1.2.1.10.7.2.1.19"  # 1 unknown, 2 half, 3 full

# ENTITY-MIB entPhysicalTable (index: entPhysicalIndex)
ENT_DESCR = "1.3.6.1.2.1.47.1.1.1.1.2"
ENT_CLASS = "1.3.6.1.2.1.47.1.1.1.1.5"  # 3 = chassis
ENT_HW_REV = "1.3.6.1.2.1.47.1.1.1.1.8"
ENT_FW_REV = "1.3.6.1.2.1.47.1.1.1.1.9"
ENT_SW_REV = "1.3.6.1.2.1.47.1.1.1.1.10"
ENT_SERIAL = "1.3.6.1.2.1.47.1.1.1.1.11"
ENT_MODEL = "1.3.6.1.2.1.47.1.1.1.1.13"

# BRIDGE-MIB (on IOS these are per VLAN: community@vlan / context vlan-N)
DOT1D_BASE_PORT_IFINDEX = "1.3.6.1.2.1.17.1.4.1.2"  # index: bridge port
DOT1D_TP_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"  # index: mac (6 sub-ids)
DOT1D_TP_FDB_STATUS = "1.3.6.1.2.1.17.4.3.1.3"  # 3 learned, 4 self, 5 mgmt

# Q-BRIDGE-MIB (index: fdbId + mac)
DOT1Q_TP_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"
DOT1Q_TP_FDB_STATUS = "1.3.6.1.2.1.17.7.1.2.2.1.3"

# CISCO-VTP-MIB vtpVlanState (index: managementDomainIndex.vlanId)
VTP_VLAN_STATE = "1.3.6.1.4.1.9.9.46.1.3.1.1.2"
# CISCO-VLAN-MEMBERSHIP-MIB vmVlan (index: ifIndex) - access VLAN of a port
VM_VLAN = "1.3.6.1.4.1.9.9.68.1.2.2.1.2"

# IP-MIB ipNetToMediaPhysAddress (index: ifIndex.a.b.c.d) - ARP cache
IP_NET_TO_MEDIA_PHYS = "1.3.6.1.2.1.4.22.1.2"

# LLDP-MIB
LLDP_LOC_PORT_ID = "1.0.8802.1.1.2.1.3.7.1.3"  # index: localPortNum
LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"
LLDP_REM_PORT_ID = "1.0.8802.1.1.2.1.4.1.1.7"  # index: timeMark.localPortNum.remIndex
LLDP_REM_PORT_DESC = "1.0.8802.1.1.2.1.4.1.1.8"
LLDP_REM_SYS_NAME = "1.0.8802.1.1.2.1.4.1.1.9"
LLDP_REM_MAN_ADDR_IF_SUBTYPE = "1.0.8802.1.1.2.1.4.2.1.3"  # index: ...remIndex.addrSubtype.len.addr

# CISCO-CDP-MIB cdpCacheTable (index: ifIndex.deviceIndex)
CDP_CACHE_ADDRESS = "1.3.6.1.4.1.9.9.23.1.2.1.1.4"
CDP_CACHE_DEVICE_ID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
CDP_CACHE_DEVICE_PORT = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"
CDP_CACHE_PLATFORM = "1.3.6.1.4.1.9.9.23.1.2.1.1.8"

IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
DUPLEX = {1: "unknown", 2: "half", 3: "full"}
ENT_CLASS_CHASSIS = 3
FDB_STATUS_LEARNED = 3
