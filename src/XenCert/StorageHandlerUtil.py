# Copyright (C) Citrix Systems Inc.
#
# This program is free software; you can redistribute it and/or modify 
# it under the terms of the GNU Lesser General Public License as published 
# by the Free Software Foundation; version 2.1 only.
#
# This program is distributed in the hope that it will be useful, 
# but WITHOUT ANY WARRANTY; without even the implied warranty of 
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the 
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

"""Storage handler classes for various storage drivers"""
import os
import re
import time
import glob
import random
import xml.dom.minidom
from XenCertLog import Print, PrintOnSameLine, XenCertPrint
from XenCertCommon import displayOperationStatus, getConfigWithHiddenPassword
import scsiutil
import util
import lvutil, vhdutil
from lvhdutil import MSIZE
import iscsilib
import mpath_cli
import mpath_dmp
import xs_errors


ISCSI_PROCNAME = "iscsi_tcp"
timeTaken = '' 
bytesCopied = ''
speedOfCopy = ''
timeLimitControlInSec = 18000

MAX_TIMEOUT = 15

KiB = 1024
MiB = KiB * KiB
GiB = KiB * KiB * KiB

SECTOR_SIZE = 1 * GiB
CHAR_SEQ = "".join([chr(x) for x in range(256)])
CHAR_SEQ_REV = "".join([chr(x) for x in range(255, -1, -1)])
BUF_PATTERN = CHAR_SEQ + CHAR_SEQ
BUF_PATTERN_REV = CHAR_SEQ_REV + CHAR_SEQ_REV
BUF_ZEROS = "\0" * 512

DISKDATATEST = '/opt/xensource/debug/XenCert/diskdatatest'
DDT_SECTOR_SIZE = 512           # one sector size: 512 bytes
DDT_DEFAULT_BLOCK_SIZE = 512    # one block size: 512 sectors, 256KB

multiPathDefaultsMap = { 'udev_dir':'/dev',
			    'polling_interval':'5',
			    'selector': "round-robin 0",
			    'path_grouping_policy':'failover',
			    'getuid_callout':"/usr/lib/udev/scsi_id --whitelisted --replace-whitespace /dev/%n",
			    'prio_callout':'none',
			    'path_checker':'readsector0',
			    'rr_min_io':'1000',
			    'rr_weight':'uniform',
			    'failback':'manual',
			    'no_path_retry':'fail',
			    'user_friendly_names':'no',
			    'bindings_file':"/var/lib/multipath/bindings" }


def _init_adapters():
    # Generate a list of active adapters
    ids = scsiutil._genHostList(ISCSI_PROCNAME)
    util.SMlog("Host ids: %s" % ids)
    adapter = {}
    for host in ids:
        try:
            if hasattr(iscsilib, 'get_targetIP_and_port'):
                (addr, port) = iscsilib.get_targetIP_and_port(host)
            else:
                addr = util.get_single_entry(glob.glob(
                    '/sys/class/iscsi_host/host%s/device/session*/connection*/iscsi_connection*/persistent_address' % host)[0])
                port = util.get_single_entry(glob.glob(
                    '/sys/class/iscsi_host/host%s/device/session*/connection*/iscsi_connection*/persistent_port' % host)[0])
            adapter[host] = (addr, port)
        except Exception, e:
            XenCertPrint("Ignore host %d IP because of exception %s" % (host, str(e)))
    return adapter

def IsMPEnabled(session, host_ref):
    try:
        hconf = session.xenapi.host.get_other_config(host_ref)
        XenCertPrint("Host.other_config: %s" % hconf)
        
        if hconf['multipathing'] == 'true' and hconf['multipathhandle'] == 'dmp':
	    return True

    except Exception, e:
	XenCertPrint("Exception determining multipath status. Exception: %s" % str(e))
    return False

def enable_multipathing(session, host):
    try:
        session.xenapi.host.remove_from_other_config(host , 'multipathing')
        session.xenapi.host.remove_from_other_config(host, 'multipathhandle')
        session.xenapi.host.add_to_other_config(host, 'multipathing', 'true')
        session.xenapi.host.add_to_other_config(host, 'multipathhandle', 'dmp')

    except Exception, e:
	XenCertPrint("Exception enabling multipathing. Exception: %s" % str(e))
    
    return

def disable_multipathing(session, host):
    try:
        session.xenapi.host.remove_from_other_config(host , 'multipathing')
        session.xenapi.host.remove_from_other_config(host, 'multipathhandle')
        session.xenapi.host.add_to_other_config(host, 'multipathing', 'false')

    except Exception, e:
	XenCertPrint("Exception disabling multipathing. Exception: %s" % str(e))
    
    return

def blockIP(ip):
    try:
	cmd = ['iptables', '-A', 'INPUT', '-s', ip, '-j', 'DROP']
        util.pread(cmd)
    except Exception, e:
        XenCertPrint("There was an exception in blocking ip: %s. Exception: %s" % (ip, str(e)))

def unblockIP(ip):
    try:
	cmd = ['iptables', '-D', 'INPUT', '-s', ip, '-j', 'DROP']
        util.pread(cmd)
    except Exception, e:
        XenCertPrint("There was an exception in unblocking ip: %s. Exception: %s" % (ip, str(e)))
   
def actualSRFreeSpace(size):
    num = (size - lvutil.LVM_SIZE_INCREMENT - 4096 - vhdutil.calcOverheadEmpty(MSIZE)) * vhdutil.VHD_BLOCK_SIZE
    den = 4096 + vhdutil.VHD_BLOCK_SIZE

    return num/den

def GetConfig(scsiid):
    try:
	retVal = True
	configMap = {}
	device = scsiutil._genReverseSCSIidmap(scsiid)[0]
	XenCertPrint("GetConfig - device: %s" % device)
	cmd = ["/usr/lib/udev/scsi_id", "--replace-whitespace", "--whitelisted", "--export", device]
	ret = util.pread2(cmd)
	XenCertPrint("GetConfig - scsi_if output: %s" % ret)
	for tuple in ret.split('\n'):
	    if tuple.find('=') != -1:
		configMap[tuple.split('=')[0]] = tuple.split('=')[1]

    except Exception, e:
	XenCertPrint("There was an exception getting SCSI device config. Exception: %s" % str(e))
	retVal = False

    return (retVal, configMap)

def findIPAddress(mapHostToIP, HBTL):
    try:
        host = HBTL.split(':')[0]
        if host in mapHostToIP and mapHostToIP[host][0]:
            return mapHostToIP[host][0]
    except Exception, e:
        XenCertPrint("Failed to find IP address for HBTL: %s, mapHostToIP: %s, exception: %s" % (HBTL, mapHostToIP, str(e)))
        raise Exception("No IP for HBTL %s in %s" % (HBTL, mapHostToIP))

# The returned structure are a list of portals, and a list of SCSIIds for the specified IQN. 
def GetListPortalScsiIdForIqn(session, server, targetIqn, chapUser = None, chapPassword = None):
    try:
	listPortal = []
	listSCSIId= []
	device_config = {}
	device_config['target'] = server
	if chapUser is not None and chapPassword is not None:
	    device_config['chapuser'] = chapUser
	    device_config['chappassword'] = chapPassword

	try:
	    session.xenapi.SR.probe(util.get_localhost_uuid(session), device_config, 'lvmoiscsi')
	except Exception, e:
	    XenCertPrint("Got the probe data as: %s" % str(e))
	    
	# Now extract the IQN list from this data.
	try:
	    # the target may not return any IQNs
	    # so prepare for it
	    items = str(e).split(',')
	    xmlstr = ''
	    for i in range(3,len(items)):
		xmlstr += items[i]
		xmlstr += ','
	    
	    #xmlstr = str(e).split(',')[3]
	    xmlstr = xmlstr.strip(',')
	    xmlstr = xmlstr.lstrip()
	    xmlstr = xmlstr.lstrip('\'')
	    xmlstr = xmlstr.rstrip()
	    xmlstr = xmlstr.rstrip('\]')
	    xmlstr = xmlstr.rstrip('\'')
	    xmlstr = xmlstr.replace('\\n', '')
	    xmlstr = xmlstr.replace('\\t', '')		
	    XenCertPrint("Got the probe xml as: %s" % xmlstr)
	    dom = xml.dom.minidom.parseString(xmlstr)
	    TgtList = dom.getElementsByTagName("TGT")		
	    for tgt in TgtList:
		iqn = None
		portal = None
		for node in tgt.childNodes:
		    if node.nodeName == 'TargetIQN':
			iqn = node.firstChild.nodeValue
		
		    if node.nodeName == 'IPAddress':
			portal = node.firstChild.nodeValue

		XenCertPrint("Got iqn: %s, portal: %s" % (iqn, portal))
		XenCertPrint("The target IQN is: %s" % targetIqn)
		if iqn == '*':
		    continue
		for targetiqn in targetIqn.split(','):
		    if iqn == targetiqn:
			listPortal.append(portal)
			break
	    
	    XenCertPrint("The portal list at the end of the iteration is: %s" % listPortal)
	except Exception, e:
	    raise Exception("The target %s did not return any IQNs on probe. Exception: %s" % (server, str(e)))
		
	#  Now probe again with each IQN in turn.
	for iqn in targetIqn.split(','):
	    try:
		device_config['targetIQN'] = iqn
		device_config_tmp = getConfigWithHiddenPassword(device_config, 'iscsi')
		XenCertPrint("Probing with device config: %s" % device_config_tmp)
		session.xenapi.SR.probe(util.get_localhost_uuid(session), device_config, 'lvmoiscsi')
	    except Exception, e:
		XenCertPrint("Got the probe data as: %s" % str(e))
    
	    # Now extract the SCSI ID list from this data.
	    try:
		# If there are no LUNs exposed, the probe data can be an empty xml
		# so be prepared for it
		items = str(e).split(',')
		xmlstr = ''
		for i in range(3,len(items)):
		    xmlstr += items[i]
		    xmlstr += ','
		#xmlstr = str(e).split(',')[3]
		xmlstr = xmlstr.strip(',')
		xmlstr = xmlstr.lstrip()
		xmlstr = xmlstr.lstrip('\'')
		xmlstr = xmlstr.rstrip()
		xmlstr = xmlstr.rstrip('\]')
		xmlstr = xmlstr.rstrip('\'')
		xmlstr = xmlstr.replace('\\n', '')
		xmlstr = xmlstr.replace('\\t', '')
		XenCertPrint("Got the probe xml as: %s" % xmlstr)
		dom = xml.dom.minidom.parseString(xmlstr)
		scsiIdObjList = dom.getElementsByTagName("SCSIid")                
		for scsiIdObj in scsiIdObjList:
		    listSCSIId.append(scsiIdObj.firstChild.nodeValue)
			
	    except Exception, e:
		XenCertPrint("The IQN: %s did not return any SCSI IDs on probe. Exception: %s" % (iqn, str(e)))
		    
	    XenCertPrint("Got the SCSIId list for iqn %s as %s" % (iqn, listSCSIId))
	    
	     
    except Exception, e: 
	XenCertPrint("There was an exception in GetListPortalScsiIdForIqn. Exception: %s" % str(e))
	raise Exception(str(e))
	
    
    XenCertPrint("GetListPortalScsiIdForIqn - returning PortalList: %s." % listPortal)  
    XenCertPrint("GetListPortalScsiIdForIqn - returning SCSIIdList: %s." % listSCSIId)  
    return (listPortal, listSCSIId)

def extract_xml_from_exception(e):
    return ','.join(str(e).split(',')[3:])

# The returned structure are a list of portals, and a list of SCSIIds for the specified IQN. 
def GetHBAInformation(session, storage_conf, sr_type="lvmohba"):
    try:
	retVal = True
	list = []
	scsiIdList = []
	device_config = {}
	HBAFilter = {}

	# Generate a map of the HBAs that the user want to test against.
	if storage_conf['adapters'] is not None:
	    for hba in storage_conf['adapters'].split(','):
			HBAFilter[hba] = 1
	
	try:
	    session.xenapi.SR.probe(util.get_localhost_uuid(session), device_config, sr_type)
	except Exception, e:
	    XenCertPrint("Got the probe data as: %s" % str(e))
	    # Now extract the HBA information from this data.
	    try:
		# the target may not return any IQNs
		# so prepare for it
		xmlstr = extract_xml_from_exception(e)
		xmlstr = xmlstr.lstrip()
		xmlstr = xmlstr.lstrip('\'')
		xmlstr = xmlstr.rstrip()
		xmlstr = xmlstr.rstrip('\]')
		xmlstr = xmlstr.rstrip('\'')
		xmlstr = xmlstr.replace('\\n', '')
		xmlstr = xmlstr.replace('\\t', '')		
		XenCertPrint("Got the probe xml as: %s" % xmlstr)
		dom = xml.dom.minidom.parseString(xmlstr)
		TgtList = dom.getElementsByTagName("Adapter")
		for tgt in TgtList:
		    map = {}
		    for node in tgt.childNodes:
			    map[node.nodeName] = node.firstChild.nodeValue
		    if len(HBAFilter) != 0:
			    if HBAFilter.has_key(map['host']):
				    list.append(map)
		    else:
			    list.append(map)
		
		bdList = dom.getElementsByTagName("BlockDevice")
		for bd in bdList:
		    for node in bd.childNodes:
			    if node.nodeName == 'SCSIid':
				    SCSIid = node.firstChild.nodeValue
			    elif node.nodeName == 'adapter':
				    adapter = ''.join(["host",node.firstChild.nodeValue])

		    if len(HBAFilter) != 0:
			    if HBAFilter.has_key(adapter):
				    scsiIdList.append(SCSIid)
		    else:
			    scsiIdList.append(SCSIid)
	
		XenCertPrint("The HBA information list being returned is: %s" % list)
	    except Exception, e:
		XenCertPrint("Failed to parse %s probe xml. Exception: %s" % (sr_type, str(e)))
	     
    except Exception, e: 
	XenCertPrint("There was an exception in GetHBAInformation: %s." % str(e))
	Print("Exception: %s" % str(e))
	retVal = False
    
    XenCertPrint("GetHBAInformation - returning adapter list: %s and scsi id list: %s." % (list, scsiIdList))  
    return (retVal, list, scsiIdList)

# the following details from the file name, put it into a list and return the list. 
def GetLunInformation(id):
    retVal = True
    listLunInfo = []
    try:
        # take in a host id, then list all files in /dev/disk/by_scsibus of the form *-5* then extract
        list = glob.glob('/dev/disk/by-scsibus/*-%s:*' % id)
        if len(list) == 0:
            retVal = False
        else:
            for file in list:
                map = {}
                basename = os.path.basename(file)
                map['SCSIid'] = basename.split('-')[0]
                map['id'] = basename.split('-')[1].split(':')[3]
                map['device'] = os.path.realpath(file)
                listLunInfo.append(map)
    except Exception, e:
        Print("Failed to get lun information for host id: %s, error: %s" % (id, str(e)))
        retVal = False

    return (retVal, listLunInfo)
	    
def PlugAndUnplugPBDs(session, sr_ref, count):
    PrintOnSameLine("      Unplugging and plugging PBDs over %d iterations. Iteration number: " % count)
    try:
	checkPoint = 0;
	for j in range(0, count):
	    PrintOnSameLine(str(j))
	    PrintOnSameLine('..')
	    pbds = session.xenapi.SR.get_PBDs(sr_ref)
	    XenCertPrint("Got the list of pbds for the sr %s as %s" % (sr_ref, pbds))
	    for pbd in pbds:
		XenCertPrint("Looking at PBD: %s" % pbd)
		session.xenapi.PBD.unplug(pbd)
		session.xenapi.PBD.plug(pbd)
	    checkPoint += 1

	PrintOnSameLine('\b\b  ')
	PrintOnSameLine('\n')
    except Exception, e:
	Print("     Exception: %s" % str(e))
	displayOperationStatus(False)
	
    displayOperationStatus(True)
    return checkPoint

def DestroySR(session, sr_ref):	
    try:
	# First get the PBDs
	pbds = session.xenapi.SR.get_PBDs(sr_ref)
	XenCertPrint("Got the list of pbds for the sr %s as %s" % (sr_ref, pbds))
	XenCertPrint(" - Now unplug PBDs for the SR.")
	for pbd in pbds:
	    XenCertPrint("Unplugging PBD: %s" % pbd)
	    session.xenapi.PBD.unplug(pbd)	    

	XenCertPrint("Now destroying the SR: %s" % sr_ref)
	session.xenapi.SR.destroy(sr_ref)
	displayOperationStatus(True)
	
    except Exception, e:
	displayOperationStatus(False)
	raise Exception(str(e))
    
def CreateMaxSizeVDIAndVBD(session, sr_ref):
    vdi_ref = None
    vbd_ref = None
    retVal = True
    vdi_size = 0
    
    try:
	try:
	    Print("   Create a VDI on the SR of the maximum available size.")
	    session.xenapi.SR.scan(sr_ref)
	    pSize = session.xenapi.SR.get_physical_size(sr_ref)
	    pUtil = session.xenapi.SR.get_physical_utilisation(sr_ref)
	    vdi_size_act = actualSRFreeSpace(int(pSize) - int(pUtil))
	    vdi_size = str(min(1073741824, vdi_size_act)) # 1073741824 is by wkc hack (1GB)
	    XenCertPrint("Actual SR free space: %d, and used VDI size %s" % (vdi_size_act, vdi_size))

	    # Populate VDI args
	    args={}
	    args['name_label'] = 'XenCertTestVDI'
	    args['SR'] = sr_ref
	    args['name_description'] = ''
	    args['virtual_size'] = vdi_size
	    args['type'] = 'user'
	    args['sharable'] = False
	    args['read_only'] = False
	    args['other_config'] = {}
	    args['sm_config'] = {}
	    args['xenstore_data'] = {}
	    args['tags'] = []            
	    XenCertPrint("The VDI create parameters are %s" % args)
	    vdi_ref = session.xenapi.VDI.create(args)
	    XenCertPrint("Created new VDI %s" % vdi_ref)
	    displayOperationStatus(True)
	except Exception, e:	    
	    displayOperationStatus(False)
	    raise Exception(str(e))

	Print("   Create a VBD on this VDI and plug it into dom0")
	try:
	    vm_uuid = _get_localhost_uuid()
	    XenCertPrint("Got vm_uuid as %s" % vm_uuid)
	    vm_ref = session.xenapi.VM.get_by_uuid(vm_uuid)
	    XenCertPrint("Got vm_ref as %s" % vm_ref)

	
	    freedevs = session.xenapi.VM.get_allowed_VBD_devices(vm_ref)
	    XenCertPrint("Got free devs as %s" % freedevs)
	    if not len(freedevs):		
		raise Exception("No free devs found for VM: %s!" % vm_ref)
	    XenCertPrint("Allowed devs: %s (using %s)" % (freedevs, freedevs[0]))

	    # Populate VBD args
	    args={}
	    args['VM'] = vm_ref
	    args['VDI'] = vdi_ref
	    args['userdevice'] = freedevs[0]
	    args['bootable'] = False
	    args['mode'] = 'RW'
	    args['type'] = 'Disk'
	    args['unpluggable'] = True 
	    args['empty'] = False
	    args['other_config'] = {}
	    args['qos_algorithm_type'] = ''
	    args['qos_algorithm_params'] = {}
	    XenCertPrint("The VBD create parameters are %s" % args)
	    vbd_ref = session.xenapi.VBD.create(args)
	    XenCertPrint("Created new VBD %s" % vbd_ref)
	    session.xenapi.VBD.plug(vbd_ref)

	    displayOperationStatus(True)
	except Exception, e:
	    displayOperationStatus(False)
	    raise Exception(str(e))
    except Exception, e:
	Print("   Exception creating VDI and VBD, and plugging it into Dom-0 for SR: %s" % sr_ref)
	raise Exception(str(e))
    
    return (retVal, vdi_ref, vbd_ref, vdi_size)

def Attach_VDI(session, vdi_ref, vm_ref):
    vbd_ref = None

    try:
        Print("   Create a VBD on the VDI and plug it into VM requested")
        freedevs = session.xenapi.VM.get_allowed_VBD_devices(vm_ref)
        XenCertPrint("Got free devs as %s" % freedevs)
        if not len(freedevs):
            err_str = "No free devs found for VM: %s!" % vm_ref
            XenCertPrint(err_str)
            raise Exception(err_str)
        XenCertPrint("Allowed devs: %s (using %s)" % (freedevs, freedevs[0]))

        # Populate VBD args
        args = {}
        args['VM'] = vm_ref
        args['VDI'] = vdi_ref
        args['userdevice'] = freedevs[0]
        args['bootable'] = False
        args['mode'] = 'RW'
        args['type'] = 'Disk'
        args['unpluggable'] = True
        args['empty'] = False
        args['other_config'] = {}
        args['qos_algorithm_type'] = ''
        args['qos_algorithm_params'] = {}
        XenCertPrint("The VBD create parameters are %s" % args)

        vbd_ref = session.xenapi.VBD.create(args)
        session.xenapi.VBD.plug(vbd_ref)
        XenCertPrint("Created new VBD %s" % vbd_ref)

        return vbd_ref

    except Exception, e:
        Print("   Exception Creating VBD and plugging it into VM: %s" % vm_ref)
        raise

def Detach_VDI(session, vbd_ref):
    try:
        session.xenapi.VBD.unplug(vbd_ref)
        XenCertPrint("Unplugged VBD %s" % vbd_ref)
        session.xenapi.VBD.destroy(vbd_ref)
        XenCertPrint("Destroyed VBD %s" % vbd_ref)
    except Exception as e:
        raise Exception('VDI detach failed. Error: %s' % e)

def FindTimeToWriteData(devicename, sizeInMiB):
    ddOutFile = 'of=' + devicename
    XenCertPrint("Now copy %dMiB data from /dev/zero to this device and record the time taken to copy it." % sizeInMiB)
    cmd = ['dd', 'if=/dev/zero', ddOutFile, 'bs=4096', 'count=%d' % (sizeInMiB * 256)]
    try:
	(rc, stdout, stderr) = util.doexec(cmd,'')
	list = stderr.split('\n')
	timeTaken = list[2].split(',')[1]
	dataCopyTime = int(float(timeTaken.split()[0]))
	XenCertPrint("The IO test returned rc: %s stdout: %s, stderr: %s" % (rc, stdout, stderr))
	XenCertPrint("Time taken to copy %dMiB to the device %s is %d" % (sizeInMiB, devicename, dataCopyTime))
	return dataCopyTime
    except Exception, e:
	raise Exception(str(e))
		
def PerformSRControlPathTests(session, sr_ref):
    e = None
    try:
	checkPoint = 0
	vdi_ref = None
	vbd_ref = None
	retVal = True	
	
	(retVal, vdi_ref, vbd_ref, vdi_size) = CreateMaxSizeVDIAndVBD(session, sr_ref)
	if not retVal:
	    raise Exception("Failed to create max size VDI and VBD.")
	
	checkPoint += 2
	# Now try to zero out the entire disk 
	Print("   Now attempt to write the maximum number of bytes on this newly plugged device.")
	
	devicename = '/dev/' + session.xenapi.VBD.get_device(vbd_ref)
	XenCertPrint("First finding out the time taken to write 1GB on the device.")
	timeFor512MiBSec = FindTimeToWriteData(devicename, 512)
	timeToWrite = int((float(vdi_size)/(1024*1024*1024)) * (timeFor512MiBSec * 2))
		
	if timeToWrite > timeLimitControlInSec:
	    raise Exception("Writing through this device will take more than %s hours, please use a source upto %s GiB in size." %
			    (timeLimitControlInSec/3600, timeLimitControlInSec/(timeFor512MiBSec * 2)))
	minutes = 0
	hrs = 0
	if timeToWrite > 60:
	    minutes = int(timeToWrite/60)
	    timeToWrite = int(timeToWrite - (minutes * 60))
	    if minutes > 60:
		hrs = int(minutes/60)
		minutes = int(minutes - (hrs * 60))
	
	Print("   START TIME: %s " % (time.asctime(time.localtime())))
	
	if hrs > 0:
	    Print("   APPROXIMATE RUN TIME: %s hours, %s minutes, %s seconds." % (hrs, minutes, timeToWrite))
	elif minutes > 0:
	    Print("   APPROXIMATE RUN TIME: %s minutes, %s seconds." % (minutes, timeToWrite))
	elif timeToWrite > 0:
	    Print("   APPROXIMATE RUN TIME: %s seconds." % (timeToWrite))
	
	ddOutFile = 'of=' + devicename
	bytes = 0
	if not util.zeroOut(devicename, 1, int(vdi_size)):	    
	    raise Exception("   - Could not write through the allocated disk space on test disk, please check the log for the exception details.")
	    
	Print("   END TIME: %s " % (time.asctime(time.localtime())))
	displayOperationStatus(True)

	checkPoint += 1
	
    except Exception, e:
	Print("There was an exception performing control path stress tests. Exception: %s" % str(e))
	retVal = False
    
    try:
	# Try cleaning up here
	if vbd_ref is not None:
	    session.xenapi.VBD.unplug(vbd_ref)
	    XenCertPrint("Unplugged VBD %s" % vbd_ref)
	    session.xenapi.VBD.destroy(vbd_ref)
	    XenCertPrint("Destroyed VBD %s" % vbd_ref)

	if vdi_ref is not None:
	    session.xenapi.VDI.destroy(vdi_ref)
	    XenCertPrint("Destroyed VDI %s" % vdi_ref)
    except Exception, e:
	Print("- Could not cleanup the objects created during testing, please destroy the vbd %s and vdi %s manually." % (vbd_ref, vdi_ref))
	Print("  Exception: %s" % str(e))
	
    return (checkPoint, retVal)

def get_lun_scsiid_devicename_mapping(targetIQN, portal):
    iscsilib.refresh_luns(targetIQN, portal)
    lunToScsiId={}
    path = os.path.join("/dev/iscsi",targetIQN,portal)
    try:
        for file in util.listdir(path):
            realPath = os.path.realpath(os.path.join(path, file))
            if file.find("LUN") == 0 and file.find("_") == -1:		
                lun=file.replace("LUN","")
                scsi_id = scsiutil.getSCSIid(os.path.join(path, file))
                lunToScsiId[lun] = (scsi_id, realPath)

        return lunToScsiId
    except util.CommandException, inst:
        XenCertPrint("Failed to find any LUNs for IQN: %s and portal: %s" % (targetIQN, portal))
        return {}

def parse_multipathd_config(lines):
    """
    Convert multipathd config to dict
    :param lines: output lines of "/usr/sbin/multipathd show config", 
        for structure refer to https://linux.die.net/man/5/multipath.conf
    :return: a dict like:
        section: [
            (attribute, value),
            ...
            (subsection, [
                (attribute, value),
                ...
            ]),
            ...
        ],
        ...
    """
    dict = {}
    re_section_begin = re.compile(r'^([^\t ]+) {\n$')
    re_section_end = re.compile(r'^}\n$')
    re_section_attr = re.compile(r'^\t([^\t ]+) (.*[^{])\n$')
    re_subsection_begin = re.compile(r'^\t([^\t ]+) {\n$')
    re_subsection_end = re.compile(r'^\t}\n$')
    re_subsection_attr = re.compile(r'^\t\t([^\t ]+) (.*[^{])\n$')
    
    for line in lines:
        m = re_section_begin.match(line)
        if m:
            section = m.group(1)
            section_value = []
            continue
        m = re_section_attr.match(line)
        if m:
            attribute,value = m.group(1),m.group(2)
            section_value.append((attribute,value))
            continue
        m = re_subsection_begin.match(line)
        if m:
            subsection = m.group(1)
            subsection_value = []
            continue
        m = re_subsection_attr.match(line)
        if m:
            attribute,value = m.group(1),m.group(2)
            subsection_value.append((attribute,value))
            continue
        m = re_subsection_end.match(line)
        if m:
            section_value.append((subsection,subsection_value))
            continue
        m = re_section_end.match(line)
        if m:
            dict[section] = section_value
            continue
       # ignore any other line
       
    return dict

def parse_config(vendor, product):
    device_config = None
    try:
        cmd="show config"
        XenCertPrint("mpath cmd: %s" % cmd)
        (rc,stdout,stderr) = util.doexec(mpath_cli.mpathcmd,cmd)
        XenCertPrint("mpath output: %s" % stdout)
        d = parse_multipathd_config([line+'\n' for line in stdout.split('\n')])
        XenCertPrint("mpath config to dict: %s" % d)

        for _,device_value in d["devices"]:
            XenCertPrint("device attributes: %s" % device_value)
            attr_map = dict(device_value)
            if 'vendor' not in attr_map or 'product' not in attr_map:
                XenCertPrint("warning: skip the device attributes because can not find mandatory key vendor or product")
                continue
            re_vendor = re.compile(attr_map['vendor'].strip('"'))
            re_product = re.compile(attr_map['product'].strip('"'))
            if (re_vendor.search(vendor) and re_product.search(product)):
                XenCertPrint("matched vendor and product")
                device_config = dict(multiPathDefaultsMap.items() + attr_map.items())
                break
    except Exception, e:
        XenCertPrint("Failed to get multipath config for vendor: %s and product: %s. Exception: %s" % (vendor, product, str(e)))

    return (device_config != None, device_config)

def parse_xml_config(file):
    configuration = {}
    # predefines if not overriden in config file
    configuration['lunsize'] = '128'
    configuration['growsize'] = '4'

    config_info = xml.dom.minidom.parse(file)
    required = ['adapterid','ssid', 'spid', 'username', 'password', 'target']
    optional = ['port', 'protocol', 'chapuser', 'chappass', 'lunsize', 'growsize']
    for val in required + optional:
       try:
           configuration[val] = str(config_info.getElementsByTagName(val)[0].firstChild.nodeValue)
       except:
           if val in required:
               print "parse exception on REQUIRED ISL option: %s" % val
               raise
           else:
               print "parse exception on OPTIONAL ISL option: %s" % val
    return configuration

#Returns a list of following tuples for the SCSI Id given
#(HBTL, Path dm status, Path status) 
def get_path_status(scsi_id, onlyActive = False):
    listPaths = []
    list = []
    retVal = True
    try:
        lines = mpath_cli.get_topology(scsi_id)
        listPaths = []
        for line in lines:
            m=mpath_cli.regex.search(line)
            if(m):
                listPaths.append(line)

        XenCertPrint("list_paths returned: %s" % listPaths)

        # Extract hbtl, dm and path status from the multipath topology output
        # e.g. "| |- 0:0:0:0 sda 8:0   active ready running"
        pat = re.compile(r'(\d+:\d+:\d+:\d+.*)$')

        for node in listPaths:
            XenCertPrint("Looking at node: %s" % node)
            match_res = pat.search(node)
            if match_res is None:
                continue

            # Extract path info if pattern matched successfully
            l = match_res.group(1).split()
            hbtl = l[0]
            dm_status = l[3]
            path_status = l[4]
            XenCertPrint("HBTL: %s" % hbtl)
            XenCertPrint("Path status: %s, %s" % (dm_status, path_status))

            if onlyActive:
                if dm_status == 'active':
                    list.append((hbtl, dm_status, path_status))
            else:
                list.append((hbtl, dm_status, path_status))

        XenCertPrint("Returning list: %s" % list)
    except Exception, e:
        XenCertPrint("There was some exception in getting path status for scsi id: %s. Exception: %s" % (scsi_id, str(e)))
        retVal = False

    return (retVal, list)

def _get_localhost_uuid():
    filename = '/etc/xensource-inventory'
    try:
        f = open(filename, 'r')
    except:
        raise xs_errors.XenError('EIO', \
              opterr="Unable to open inventory file [%s]" % filename)
    domid = ''
    for line in filter(util.match_domain_id, f.readlines()):
        domid = line.split("'")[1]
    return domid

def DiskDataTest(device, test_blocks, sect_of_block=DDT_DEFAULT_BLOCK_SIZE, test_time=0):
    iter_start = str(random.randint(0, 100000))
    
    cmd = [DISKDATATEST, 'write', device, str(sect_of_block), str(test_blocks), str(test_time), iter_start]
    XenCertPrint("The command to be fired is: %s" % cmd)
    (rc, stdout, stderr) = util.doexec(cmd)
    if rc != 0:
        raise Exception("Disk test write error!")

    XenCertPrint("diskdatatest returned : %s" % stdout)
    lastString = stdout.strip().splitlines()[-1]
    total_blocks, write_blocks, write_elapsed, _ = lastString.split()
    total_blocks, write_blocks, write_elapsed = int(total_blocks), int(write_blocks), float(write_elapsed)

    cmd = [DISKDATATEST, 'verify', device, str(sect_of_block), str(write_blocks), str(test_time), iter_start]
    XenCertPrint("The command to be fired is: %s" % cmd)
    (rc, stdout, stderr) = util.doexec(cmd)
    if rc != 0:
        raise Exception("Disk test verify error!")

    XenCertPrint("diskdatatest returned : %s" % stdout)
    lastString = stdout.strip().splitlines()[-1]
    _, verify_blocks, verify_elapsed, sector_errors = lastString.split()
    verify_blocks, verify_elapsed, sector_errors = int(verify_blocks), float(verify_elapsed), int(sector_errors)

    if sector_errors != 0:
        raise Exception("Disk test verify error on %d sectors!", sector_errors)
        
    return total_blocks, write_blocks, write_elapsed, verify_blocks, verify_elapsed
    
def GetBlocksNum(size, sect_of_block=DDT_DEFAULT_BLOCK_SIZE):
    return size*MiB/(sect_of_block*DDT_SECTOR_SIZE)
    
def FindDiskDataTestEstimate(device, size):
    # Run diskdatatest in a report mode
    XenCertPrint("Run diskdatatest in a report mode with device %s to find the estimated time." % device)

    total_blocks, write_blocks, write_elapsed, verify_blocks, verify_elapsed = \
            DiskDataTest(device, GetBlocksNum(size), test_time=15)

    estimatedTime = total_blocks * (write_elapsed/write_blocks + verify_elapsed/verify_blocks)
 
    XenCertPrint("Total estimated time for testing IO with the device %s as %d" % (device, estimatedTime))
    return estimatedTime

def _find_LUN(svid):
    basepath = "/dev/disk/by-csldev/"
    if svid.startswith("NETAPP_"):
        # special attention for NETAPP SVIDs
        svid_parts = svid.split("__")
        globstr = basepath + "NETAPP__LUN__" + "*" + svid_parts[2] + "*" + svid_parts[-1] + "*"
    else:
        globstr = basepath + svid + "*"

    path = util.wait_for_path_multi(globstr, MAX_TIMEOUT)
    if not len(path):
        return []

    #Find CSLDEV paths
    svid_to_use = re.sub("-[0-9]*:[0-9]*:[0-9]*:[0-9]*$","",os.path.basename(path))
    devs = scsiutil._genReverseSCSIidmap(svid_to_use, pathname="csldev")

    #Find scsiID
    for dev in devs:
        try:
            SCSIid = scsiutil.getSCSIid(dev)
        except:
            pass

    #Find root device and return
    if not SCSIid:
        return []
    else:
        device=mpath_dmp.path(SCSIid)
        XenCertPrint("DEBUG: device path : %s" % (device))
        return [device]

def WriteDataToVDI(session, vbd_ref, startSec, endSec):
    XenCertPrint('WriteDataToVDI(vbd_ref=%s, startSec=%s, endSec=%s, ->Enter)' \
                 % (vbd_ref, startSec, endSec))
    try:
        device = os.path.join('/dev/', session.xenapi.VBD.get_device(vbd_ref))

        XenCertPrint('about to write onto device: %s' % device)

        with open(device, 'w+') as f:
            while startSec <= endSec:
                f.seek(startSec * SECTOR_SIZE)
                f.write(BUF_PATTERN)
                startSec += 1
    except Exception, e:
        raise Exception('Writing data into VDI:%s Failed. Error: %s' \
                % (vbd_ref, e))

    XenCertPrint('WriteDataToVDI() -> Exit')

def VerifyDataOnVDI(session, vbd_ref, startSec, endSec):
    XenCertPrint('VerifyDataOnVDI(vdi_ref=%s, startSec=%s, endSec=%s ->Enter)' \
                 % (vbd_ref, startSec, endSec))
    try:
        device = os.path.join('/dev/', session.xenapi.VBD.get_device(vbd_ref))

        XenCertPrint('about to read from device: %s' % device)

        expect = BUF_PATTERN

        with open(device, 'r+') as f:
            while startSec <= endSec:
                f.seek(startSec * SECTOR_SIZE)
                actual = f.read(len(expect))
                if actual != expect:
                    raise Exception('expected:%s != actual:%s'\
                             % (expect, actual))
                startSec += 1
    except Exception, e:
        raise Exception('Verification of data in VDI:%s Failed. Error:%s'\
                % (vbd_ref, e))

    XenCertPrint('VerifyDataOnVDI() -> Exit')
