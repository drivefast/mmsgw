import os
import syslog
import configparser

API_ROOT = os.path.dirname(os.path.realpath(__file__)) + "/"

TEMPORARY_FILES_DIR = "/tmp/"

LOG_IDENT = "mmsgw"
LOG_FACILITY = syslog.LOG_LOCAL4

ACCEPTED_MESSAGE_PRIORITIES = ("low", "normal", "high")
ACCEPTED_MESSAGE_CLASSES = ("personal", "advertisement", "informational", "auto")
ACCEPTED_CONTENT_CLASSES = ("text", "image-basic", "image-rich", "video-basic", "video-rich", "megapixel", "content-basic", "content-rich")
ACCEPTED_CONTENT_TYPES = (
    "application/smil",
    "text/plain", 
    "image/bmp", "image/gif", "image/jpeg", "image/tiff", "image/png",
    "audio/basic", "audio/mid", "audio/mpeg", "audio/mp4", "audio/wav",
)
ACCEPTED_CHARGED_PARTY = ( "sender", "recipient", "both", "neither" )

CFG_ROOT = "/etc/mmsgw/"
cfg = configparser.ConfigParser()
cfg.read(CFG_ROOT + "mmsgw.conf")

API_URL = cfg['general']['api_url']
API_DEV_PORT = int(cfg['general'].get('api_dev_port', 8080))

MMS_TTL = int(cfg['general'].get('mms_ttl', 3600))

GW_HEARTBEAT_TIMER = int(cfg['general'].get('gateway_heartbeat_interval', 30))
GW_HEARTBEATS = int(cfg['general'].get('gateway_max_missed_heartbeats', 10))
MAX_TX_RETRIES = int(cfg['general'].get('max_tx_retries', 5))

STORAGE_CONN = cfg['message_storage']
QUEUE_CONN = cfg['queue_storage']


