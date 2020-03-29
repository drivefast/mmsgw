import os
import syslog
import configparser

API_ROOT = os.path.dirname(os.path.realpath(__file__)) + "/"
URL_ROOT = "/mmsgw/v1/"
ENABLE_TESTS = True

LOG_IDENT = "mmsgw"
LOG_FACILITY = syslog.LOG_LOCAL4

ACCEPTED_MESSAGE_PRIORITIES = ("low", "normal", "high")
ACCEPTED_MESSAGE_CLASSES = ("personal", "advertisement", "informational", "auto")
ACCEPTED_CONTENT_CLASSES = ("text", "image-basic", "image-rich", "video-basic", "video-rich", "megapixel", "content-basic", "content-rich")
ACCEPTED_CHARGED_PARTY = ( "sender", "recipient", "both", "neither" )
ACCEPTED_CONTENT_TYPES = {
    'application/smil': ".smil",
    'text/plain': ".txt", 
    'image/bmp': ".bmp", 'image/gif': ".gif", 'image/jpeg': ".jpg", 'image/jpg': ".jpg", 'image/tiff': ".tif", 'image/png': ".png",
    'audio/basic': ".au", 'audio/mid': ".mid", 'audio/mpeg': ".mpg", 'audio/mp4': ".mp4", 'audio/wav': ".wav",
}

MM7_VERSION = {
    'mm7': "6.8.0",
    'xmlns_suffix': "REL-6-MM7-1-4",
}
MM7_NAMESPACE = {
    'env': "http://schemas.xmlsoap.org/soap/envelope/",
    'mm7': "http://www.3gpp.org/ftp/Specs/archive/23_series/23.140/schema/" + MM7_VERSION['xmlns_suffix'],
}

CFG_ROOT = "/etc/mmsgw/"
cfg = configparser.ConfigParser()
cfg.read(CFG_ROOT + "mmsgw.conf")

API_URL = cfg['general']['api_url']
API_DEV_PORT = int(cfg['general'].get('api_dev_port', 8080))
TMP_MEDIA_DIR = cfg['general'].get("media_dir", "/tmp/media/")
if not TMP_MEDIA_DIR.endswith("/"):
    TMP_MEDIA_DIR += "/"

MMS_TTL = int(cfg['general'].get('mms_ttl', 4 * 3600))
MMS_TEMPLATE_TTL = int(cfg['general'].get('mms_template_ttl', 24 * 3600))

DEFAULT_GATEWAY = cfg['general'].get('default_gateway', "provider")
GW_HEARTBEAT_TIMER = int(cfg['general'].get('gateway_heartbeat_interval', 30))
GW_HEARTBEATS = int(cfg['general'].get('gateway_max_missed_heartbeats', 10))
MAX_TX_RETRIES = int(cfg['general'].get('max_transmit_retries', 5))

STORAGE_CONN = cfg['message_storage']
QUEUE_CONN = cfg['queue_storage']


