import sys
import configparser
import socket
import os
import threading
from rq import Connection, Worker

from constants import *
from backend.logger import log
from backend.storage import rdbq
import models.gateway


def heartbeat(gateway):
    if gateway.heartbeat():
        threading.Timer(GW_HEARTBEAT_TIMER, heartbeat, [ gateway ]).start()
    else:
        # the gateway has not been able to perform its intended operation for a while
        # we forcefully kill it, so it doesn't try to process jobs anymore
        log.alarm("[{}] This gateway died____________".format(gateway.gwid))
        os._exit(1)


gw = None
if len(sys.argv) < 2:
    print("To start a gateway, use a configuration filename as a command line argument.\n")
    exit()
cfg = configparser.ConfigParser()
cfg.read(sys.argv[len(sys.argv) - 1])
gw_group = cfg['gateway'].get('group')
gw_name = cfg['gateway'].get('name')
gwid = "{}:{}:{}:{}".format(gw_group, gw_name, socket.gethostname(), os.getpid())

gw_type = cfg['gateway'].get('protocol').upper()
log.warning("[{}] Starting {} gateway".format(gwid, gw_type))
if gw_type == "MM4":
    gw = models.gateway.MM4Gateway(gwid)
elif gw_type == "MM7":
    gw = models.gateway.MM7Gateway(gwid)
else:
    print(sys.argv[len(sys.argv) - 1] + "Gateway protocol unsupported or missing; use MM4 or MM7.\n")
    exit()
gw.config(cfg)
if not gw.start():
    print("SMTP connection error, check logs. This gateway instance will not start.\n")
    exit()

models.gateway.THIS_GW = gw

burst = "-b" in sys.argv or "--burst" in sys.argv
if not burst:
    log.debug("[{}] Setting up heartbeat".format(gwid))
    heartbeat(gw)

with Connection(connection=rdbq):
    w = Worker(['QTX-' + gw_group, 'QRX-' + gw_group, 'QEV-' + gw_group], name=gwid)
    w.work()


