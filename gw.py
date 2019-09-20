import sys
import configparser
from rq import Connection, Worker

from constants import *
from backend.storage import rdbq
from models.gateway import MM4Gateway, MM7Gateway

gw = None
if len(sys.argv) < 2:
    print "To start a gateway, use the gateway ID as a command line argument.\n"
    exit()
gwid = sys.argv[1]
cfg = configparser.ConfigParser()
cfg.read(CFGROOT + "gateways/" + gwid + ".conf")
gwtype = cfg['gateway'].get('protocol').upper()
if gwtype == "MM4":
    gw = MM4Gateway(gwid)
    gw.config(cfg)
    if gw.start() is None:
        print "SMTP connection error, check logs.\n")
        exit()
elif gwtype == "MM7":
    gw = MM7Gateway(gwid)
    gw.config(cfg)
else:
    print "Gateway protocol unsupported or missing; use MM4 or MM7.\n"
    exit()

burst = "-b" in sys.argv or "--burst" in sys.argv
g = cfg['gateway'].get('group')
if g and not burst:
    gw.register(g)

with Connection(connection=rdbq):
    w = Worker(['QEV-' + gwid, 'QTX-' + gwid, 'QRX-' + gwid], name=gwid)
    w.work()


