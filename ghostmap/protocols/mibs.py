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

# CISCO-PROCESS-MIB cpmCPUTotalTable (index: cpmCPUTotalIndex, one row per CPU)
CPM_CPU_5SEC_REV = "1.3.6.1.4.1.9.9.109.1.1.1.1.6"  # percent, last 5 seconds
CPM_CPU_1MIN_REV = "1.3.6.1.4.1.9.9.109.1.1.1.1.7"
CPM_CPU_5MIN_REV = "1.3.6.1.4.1.9.9.109.1.1.1.1.8"
CPM_CPU_5SEC = "1.3.6.1.4.1.9.9.109.1.1.1.1.3"  # older IOS (deprecated columns, same meaning)
CPM_CPU_1MIN = "1.3.6.1.4.1.9.9.109.1.1.1.1.4"
CPM_CPU_5MIN = "1.3.6.1.4.1.9.9.109.1.1.1.1.5"
CPM_CPU_MEM_USED = "1.3.6.1.4.1.9.9.109.1.1.1.1.12"  # kilobytes (IOS XE)
CPM_CPU_MEM_FREE = "1.3.6.1.4.1.9.9.109.1.1.1.1.13"

# CISCO-MEMORY-POOL-MIB ciscoMemoryPoolTable (index: pool type; 1 = Processor)
MEM_POOL_NAME = "1.3.6.1.4.1.9.9.48.1.1.1.2"
MEM_POOL_USED = "1.3.6.1.4.1.9.9.48.1.1.1.5"  # bytes
MEM_POOL_FREE = "1.3.6.1.4.1.9.9.48.1.1.1.6"

# CISCO-ENVMON-MIB (index: sensor / fan / supply index)
ENV_TEMP_DESCR = "1.3.6.1.4.1.9.9.13.1.3.1.2"
ENV_TEMP_VALUE = "1.3.6.1.4.1.9.9.13.1.3.1.3"  # degrees Celsius
ENV_TEMP_THRESHOLD = "1.3.6.1.4.1.9.9.13.1.3.1.4"
ENV_TEMP_STATE = "1.3.6.1.4.1.9.9.13.1.3.1.6"
ENV_FAN_DESCR = "1.3.6.1.4.1.9.9.13.1.4.1.2"
ENV_FAN_STATE = "1.3.6.1.4.1.9.9.13.1.4.1.3"
ENV_SUPPLY_DESCR = "1.3.6.1.4.1.9.9.13.1.5.1.2"
ENV_SUPPLY_STATE = "1.3.6.1.4.1.9.9.13.1.5.1.3"
ENV_STATE = {1: "normal", 2: "warning", 3: "critical", 4: "shutdown", 5: "notPresent", 6: "notFunctioning"}

# ENTITY-SENSOR-MIB entPhySensorTable (index: entPhysicalIndex), used when ENVMON has no temperatures
ENT_PHYSICAL_NAME = "1.3.6.1.2.1.47.1.1.1.1.7"
ENT_SENSOR_TYPE = "1.3.6.1.2.1.99.1.1.1.1"  # 8 = celsius
ENT_SENSOR_SCALE = "1.3.6.1.2.1.99.1.1.1.2"  # 9 = units; each step is a factor of 1000
ENT_SENSOR_PRECISION = "1.3.6.1.2.1.99.1.1.1.3"
ENT_SENSOR_VALUE = "1.3.6.1.2.1.99.1.1.1.4"
ENT_SENSOR_STATUS = "1.3.6.1.2.1.99.1.1.1.5"  # 1 ok, 2 unavailable, 3 nonoperational
ENT_SENSOR_CELSIUS = 8
ENT_SENSOR_STATUS_NAMES = {1: "normal", 2: "unavailable", 3: "notFunctioning"}

# BRIDGE-MIB dot1dStp scalars (on IOS per VLAN: community@vlan / context vlan-N)
STP_TIME_SINCE_CHANGE = "1.3.6.1.2.1.17.2.3"  # .0, hundredths of a second
STP_TOP_CHANGES = "1.3.6.1.2.1.17.2.4"  # .0, count since the switch started
STP_DESIGNATED_ROOT = "1.3.6.1.2.1.17.2.5"  # .0, 8 bytes: priority (2) + MAC (6)

IF_STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}
DUPLEX = {1: "unknown", 2: "half", 3: "full"}
ENT_CLASS_CHASSIS = 3
FDB_STATUS_LEARNED = 3
