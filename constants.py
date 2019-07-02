import os
import syslog
import configparser

API_ROOT = os.path.dirname(os.path.realpath(__file__)) + "/"

LOG_IDENT = "mmsgw"
LOG_FACILITY = syslog.LOG_LOCAL6

ACCEPTED_CONTENT_TYPES = [
    "text/plain", "application/smil",
    "image/bmp", "image/gif", "image/jpeg", "image/tiff", "image/png",
    "audio/basic", "audio/mid", "audio/mpeg", "audio/mp4", "audio/wav",
]

cfg = configparser.ConfigParser()
cfg.read("/etc/mmsgw/mmsgw.conf")

API_URL = cfg['general']['api_url']
API_DEV_PORT = int(cfg['general'].get('api_dev_port', 8080))

MMS_TTL = int(cfg['general'].get('mms_ttl', 3600))

STORAGE_CONN = cfg['message_storage']
QUEUE_CONN = cfg['queue_storage']


