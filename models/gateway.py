import os.path
import shutil
import time
import datetime
import uuid
import base64
import rq
import requests
import xmltodict

import traceback

import xml.etree.cElementTree as ET
import smtplib
import email, email.utils
import mimetypes
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.audio import MIMEAudio

from constants import *
from backend.logger import log
from backend.util import find_in_dict, download_to_file, repo
from backend.storage import rdb, rdbq
import models.transaction
import models.message

MM7_ROOT_NS = "soapenv"
MM7_STATUS = {
    '1000': "Success",
    '1100': "Partial success",
    '2000': "Client error",
    '2001': "Operation restricted",
    '2002': "Address Error",
    '2003': "Address Not Found",
    '2004': "Multimedia content refused",
    '2005': "Message ID Not found",
    '2006': "LinkedID not found",
    '2007': "Message format corrupt",
    '2008': "Application ID not found",
    '2009': "Reply Application ID not found",
    '3000': "Server Error",
    '3001': "Not Possible",
    '3002': "Message rejected",
    '3003': "Multiple addresses not supported",
    '3004': "Application Addressing not supported",
    '4000': "General service error",
    '4001': "Improper identification",
    '4002': "Unsupported version",
    '4003': "Unsupported operation",
    '4004': "Validation error",
    '4005': "Service error",
    '4006': "Service unavailable",
    '4007': "Service denied",
    '4008': "Application denied",
}
TOP_PART_BOUNDARY = "========Top-Part-Boundary"

THIS_GW = None

def send_mms(txid):
    tx = models.transaction.MMSTransaction(txid)
    if tx is None:
        log.warning("[{}] transaction {} not found when attempting to send"
            .format(gw.gwid, txid)
        )
        return
    gw = THIS_GW
    log.debug("[{}] processing transaction {}".format(gw.gwid, txid))
    tx.gateway_id += gw.gwid + " "
    tx.processed_ts = int(time.time())
    tx.save()

    # is this gateway healthy enough to execute the job?
    this_job = rq.get_current_job()
    heartbeats_left = rdbq.get('gwstat-' + gw.gwid) 
    if heartbeats_left is None:
        # this shouldnt happen: if the heartbeat key is gone, the gateway instance is dead
        # (if it does happen, then it's just my bad logic)
        log.alarm("[{}] gateway still alive, despite missing {} heartbeats"
            .format(gw.gwid, GW_HEARTBEATS - heartbeats_left)
        )
        reschedule(this_job, gw.q_tx)
    elif heartbeats_left < (GW_HEARTBEATS - 1):
        # is this gateway is probably not healthy enough to process the job, have it skip it
        log.warning("[{}] gateway in bad state when attempted transmission {}, rescheduling"
            .format(gw.gwid, txid)
        )
        reschedule(this_job, gw.q_tx)
    else:

        try:
            log.debug("[{}] {} creating {} transmission image".format(gw.gwid, txid, gw.protocol))
            m = gw.render(tx)
            if m:
                log.debug("[{}] {} prepared for transmission".format(gw.gwid, txid))
                if gw.protocol == "MM4":
                    src_addr = gw.origin_prefix + tx.message.origin + gw.origin_suffix
                    if "@" not in src_addr:
                        src_addr += "@" + gw.local_domain
                    dest_addr = list(map(lambda a: 
                        gw.dest_prefix + a + gw.dest_suffix + 
                        (("@" + gw.remote_domain) if "@" not in gw.dest_suffix else "")
                    , tx.destination | tx.cc | tx.bcc))
                    ret_code, ret_desc = gw.send_to_mmsc(m, txid, src_addr, dest_addr)
                else:
                    ret_code, ret_desc = gw.send_to_mmsc(m, txid)
                if ret_code is None:
                    tx.set_state([], "SENT", "", "", gw.gwid, ret_desc)
                    return
                else:
                    log.debug("[{}] {} transmission error {}: {}".format(gw.gwid, txid, ret_code, ret_desc))
            else:
                ret_code, ret_desc = "1", "internal error: failed to properly render message" 
        except Exception as ex:
            log.info("[{}] {} gateway error: {}".format(gw.gwid, txid, traceback.format_exc()))
            ret_code, ret_desc = "2", "internal error: {}".format(ex) 

#        # callback to the app if necessary
#        q_cb = rq.Queue("QCB", connection=rdbq)
#        url_list = tx.report_url.split(",") + gw.report_url.split(",")
#        for url in url_list:
#            q_cb.enqueue_call(func='models.transmission.send_event', args=( url, txid, ))

        tx.set_state([], "FAILED", ret_code.zfill(4), ret_desc, gw.gwid, ret_desc)
        if len(ret_code) > 1:
            # only reschedule for external, environmental errors
            reschedule(this_job, gw.q_tx)


def mm4rx(fn):
    # handle received MM4 SMTP message
    gw = THIS_GW
    jid = rq.get_current_job().id
    res = None
    try:
        with open(fn, "r") as fh:
            m = email.message_from_file(fh)
            if m["X-Mms-3GPP-MMS-Version"] is None:
                log.warning("[{}] {} not an MM4 message".format(gw.gwid, jid))
                return
            m_type = m["X-Mms-Message-Type"]
            log.debug("[{}] {} handling as {} MM4 message".format(gw.gwid, jid, m_type))
            if m_type is None:
                log.info("[{}] {} has no MM4 message type".format(gw.gwid, jid))
            elif m_type.lower() == "mm4_forward.req":
                res = gw.process_mo(m)
            elif m_type.lower() == "mm4_forward.res":
                res = gw.process_mt_ack(m)
            elif m_type.lower() == "mm4_delivery_report.req":
                res = gw.process_dlr(m)
            elif m_type.lower() == "mm4_read_reply_report.req":
                res = gw.process_rrr(m)
            else:
                log.info("[{}] {} unhandled MM4 message type '{}'".format(gw.gwid, jid, m_type))
    except IOError as ioe:
        log.info("[{}] MM4 error reading content of {} at {}: {}".format(gw.gwid, jid, fn, ioe))
    except email.errors.MessageParseError as mpe:
        log.info("[{}] MM4 content of {} could not be parsed: {}".format(gw.gwid, jid, mpe))
    except Exception as e:
        log.info("[{}] internal error handling received MM4 {}: {}"
            .format(gw.gwid, jid, traceback.format_exc())
        )

    if res is not None:
        pass


def process_event(txid, meta):
    gw = THIS_GW
    

def process_mo(txid, meta, content):
    gw = THIS_GW
    msg, status = gw.create_message(meta)
    if msg:
        if not content.is_multipart():
            m = content.get_payload(decode=True)
            log.info("Media: {} size {}".format(
                content.get_content_type(),
                content.get("Content-Length", "unknown")
            ))
            if bad_media:
                status = '2004'
        else:
            one_bad = False; all_bad = False
            media_parts = content.get_payload()
            for mp in media_parts:
                bad_media = False
                m = mp.get_payload(decode=True)
                log.info("Media: {} size {}".format(
                    mp.get_content_type(),
                    mp.get("Content-Length", "unknown")
                ))
                one_bad = one_bad or bad_media
                all_bad = all_bad and bad_media
            if one_bad: status = '1100'
            if all_bad: status = '2004'


def reschedule(job, queue):
    job.meta['retries'] = job.meta['retries'] - 1
    if job.meta['retries'] < 0:
        log.warning("[{}] {} transaction aborted, too many retries"
            .format(THIS_GW.gwid, job.get_id())
        )
    else:
        job.save_meta()
        queue.enqueue_job(job)


class MMSGateway(object):

    gwid = None
    q_tx = None
    q_rx = None

    # gateway
    name = None
    group = None
    protocol = None
    protocol_version = None
    carrier = ""
    active = True
    tps_limit = 0
    report_url = ""

    # outbound
    secure = False
    remote_peer = None       # SMTP remote server for MM4, as ( host, port ) tuple; MMSC URL for MM7
    local_host = None        # identification of the local server (fqdn) for MM4; URL to local for MM7
    auth = None              # authentiation to use for transmitting messages, as ( user, password ) tuple
    ssl_certificate = None   # ( keyfile, certfile ) tuple, MM4 only

    # inbound
    this_domain = None   # emails coming from any of these domains will be dispatched to this gateway
    this_host = None

    # addressing
    dest_prefix = ""
    dest_suffix = ""
    origin_prefix = ""
    origin_suffix = ""

    # features
    request_ack = True
    request_dlr = True
    request_rrr = True
    applic_id = None
    reply_applic_id = None
    aux_applic_info = None


    def __init__(self, gwid):
        self.gwid = gwid
        self.group = gwid.split(":")[0]
        self.q_tx = rq.Queue("QTX-" + self.group, connection=rdbq)
        self.q_rx = rq.Queue("QRX-" + self.group, connection=rdbq)


    def config(self, cfg):
        self.group = cfg['gateway'].get('group')
        self.name = cfg['gateway'].get('name')
        self.protocol_version = cfg['gateway'].get('version')
        self.carrier = cfg['gateway'].get('carrier', "")
        self.tps_limit = int(cfg['gateway'].get('tps_limit', 0))
        self.report_url = cfg['gateway'].get('report_url', "")

        self.secure = cfg['outbound'].get('secure_connection', "").lower() in ("yes", "true", "t", "1")
        self.remote_peer = [ cfg['outbound'].get('remote_host', "localhost"), 0 ]
        self.local_host = cfg['outbound'].get('local_host', "")
        if len(cfg['outbound'].get('username', "")) > 0 and len(cfg['outbound'].get('password', "")) > 0:
            self.auth = ( cfg['outbound']['username'], cfg['outbound']['password'] )

        self.dest_prefix = cfg['addressing'].get('dest_prefix', "")
        self.dest_suffix = cfg['addressing'].get('dest_suffix', "")
        self.origin_prefix = cfg['addressing'].get('origin_prefix', "")
        self.origin_suffix = cfg['addressing'].get('origin_suffix', "")

        self.request_ack = cfg['features'].get('request_submit_ack', "").lower() in ("yes", "true", "t", "1")
        self.request_dlr = cfg['features'].get('request_delivery', "").lower() in ("yes", "true", "t", "1")
        self.request_rrr = cfg['features'].get('request_read_receipt', "").lower() in ("yes", "true", "t", "1")

        self.applic_id = cfg['features'].get('applic_id')
        self.reply_applic_id = cfg['features'].get('reply_applic_id')
        self.aux_applic_info = cfg['features'].get('aux_applic_info')


class MM4Gateway(MMSGateway):

    connection = None
    remote_domain = None
    local_domain = None
    originator_addr = None
    recipient_addr = None
    mmsip_addr = None
    forward_route = None
    return_route = None


    def __init__(self, gwid):
        super(MM4Gateway, self).__init__(gwid)
        self.protocol = "MM4"


    def config(self, cfg):
        super(MM4Gateway, self).config(cfg)
        self.remote_peer[1] = int(cfg['outbound'].get('remote_port', (465 if self.secure else 25)))
        self.remote_domain = cfg['outbound'].get('remote_domain')
        self.local_domain = cfg['outbound'].get('local_domain')
        self.originator_addr = cfg['outbound'].get('originator_address')
        self.recipient_addr = cfg['outbound'].get('recipient_address')
        keyfile = cfg['outbound'].get('keyfile')
        certfile = cfg['outbound'].get('certfile')
        if keyfile is not None and certfile is not None and self.secure:
            self.ssl_certificate = ( keyfile, certfile )
        self.this_domain = cfg['inbound'].get('domain')
        self.this_host = cfg['inbound'].get('host')
        self.return_route = cfg['features'].get('return_route')
        self.mmsip_addr = cfg['features'].get('mmsip_address')
        self.forward_route = cfg['features'].get('forward_route')


    def connect(self):
        # try connecting to the remote MMSC we will use for sending MTs
        log.debug("[{}] connecting to {}:{} as {}"
            .format(self.gwid, self.remote_peer[0], self.remote_peer[1], self.this_host)
        )
        try:
            if self.secure:
                self.connection = smtplib.SMTP_SSL(
                    self.remote_peer[0], self.remote_peer[1], 
                    self.this_host, 
                    self.ssl_certificate[0], self.ssl_certificate[1],
                    GW_HEARTBEAT_TIMER
                )
            else:
                self.connection = smtplib.SMTP(
                    self.remote_peer[0], self.remote_peer[1], 
                    self.this_host, 
                    GW_HEARTBEAT_TIMER
                )
            # self.connection will be None if this fails
        except smtplib.SMTPException as se:
            log.critical("[{}] Gateway connection error: {}".format(self.gwid, se))
        except Exception as e:
            log.critical("[{}] Gateway connection error: {}".format(self.gwid, e))


    def start(self):
        # start outbound gateway
        rdbq.set('gwstat-' + self.gwid, 1, 3 + GW_HEARTBEAT_TIMER)
        self.connect()
        # register the gateway to receive inbound messages
        rdbq.sadd('mmsrxsource-' + self.this_domain, self.gwid)
        rdbq.sadd('mmsrxsource-' + self.this_host, self.gwid)
        return self.connection is not None


    def heartbeat(self):
        if rdbq.decr('gwstat-' + self.gwid) <= -1:
            # this gateway bleeded to death, we need to stop the app
            rdbq.delete('gwstat-' + self.gwid)
            return False
        if self.connection is None:
            # try reconnecting
            self.connect()
            if self.connection is None:
                # connection still didnt work, set gateway in an uncertain functional state
                log.critical("[{}] Currently no connection to remote MMSC".format(self.gwid))
        try:
            rp = self.connection.docmd("HELO", self.this_host)
            if rp[0] == 250:
                rdbq.set('gwstat-' + self.gwid, GW_HEARTBEATS, 
                    ex=(GW_HEARTBEAT_TIMER * (2 + GW_HEARTBEATS))
                )
                log.debug("[{}] _/\_".format(self.gwid))
            else:
                log.critical("[{}] Remote server didn't like our HELO: {}".format(self.gwid, rp))
            return rp[0] == 250
        except smtplib.SMTPException as se:
            log.critical("[{}] Gateway heartbeat failed: {}".format(self.gwid, se))
            if se.message.startswith("Connection unexpectedly closed"):
                self.connection = None
        except AttributeError:
            pass
        return True


    def render(self, tx):

        e = MIMEMultipart("related", boundary=TOP_PART_BOUNDARY)

        e['Date'] = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")
        e['From'] = self.origin_prefix + tx.message.origin + self.origin_suffix
        e['Sender'] = self.originator_addr
        e['Subject'] = tx.message.subject
        if len(tx.destination):
            e['To'] = ",".join(map(lambda a: self.dest_prefix + a + self.dest_suffix, tx.destination))
        if len(tx.cc):
            e['Cc'] = ",".join(map(lambda a: self.dest_prefix + a + self.dest_suffix, tx.cc))
        if len(tx.bcc):
            e['Bcc'] = ",".join(map(lambda a: self.dest_prefix + a + self.dest_suffix, tx.bcc))
        
        for pid in tx.message.parts:
            p = models.message.MMSMessagePart(pid)
            if p.content:
                # actual content is provided in the part object itself
                content = p.content
            elif (
                p.content_url.startswith("file://") or
                p.content_url.startswith("http://") or 
                p.content_url.startswith("https://")
            ):
                # download the media file, unless already exists
                if not os.path.exists(content):
                    content = download_to_file(p.content_url, repo( 
                        TMP_MEDIA_DIR, tx.message.message_id + "-" + p.content_id
                    ))
            else:
                log.warning("[{}] {} failed to obtain content for part '{}' in message {}"
                    .format(self.gwid, tx.tx_id, p.content_id, tx.message.message_id)
                )
                continue
            log.debug("[{}] {} message {} part {} saved as '{}'"
                .format(self.gwid, tx.tx_id, tx.message.message_id, p.content_id, content)
            )
            mp = None
            try:
                if p.content_type == "application/smil":
                    mp = MIMEBase("application", "smil", name=p.attachment_name)
                    mp.set_payload(content)
                    e.set_param("start", p.content_id)
                elif p.content_type == "text/plain":
                    mp = MIMEText(content)
                elif p.content_type.startswith("image/"):
                    fh = open(content)
                    mp = MIMEImage(fh.read())
                    fh.close()
                elif p.content_type.startswith("audio/"):
                    fh = open(content)
                    mp = MIMEAudio(fh.read())
                    fh.close()
            except Exception as e:
                log.warning("[{}] {} failed to create MIME part '{}' component for message {}: {}"
                    .format(self.gwid, tx.tx_id, p.content_id, tx.message.message_id, e)
                )
                mp = None
            if mp:
                mp.add_header("Content-Id", p.content_id)
                e.attach(mp)

        e.add_header("X-Mms-3GPP-MMS-Version", self.protocol_version)
        e.add_header("X-Mms-Message-Type", "MM4_forward.REQ")
        e.add_header("X-Mms-Transaction-ID", tx.last_req_id)
        e.add_header("X-Mms-Message-ID", tx.tx_id)
        if tx.message.expire_after > 0:
            e.add_header("X-Mms-Expiry", tx.message.expire_after)
        if tx.message.message_class:
            e.add_header("X-Mms-Message-Class", tx.message.message_class)
        if tx.priority:
            e.add_header("X-Mms-Priority", tx.priority)
        if self.request_ack:
            e.add_header("X-Mms-Ack-Request", "Yes")
            e.add_header("X-Mms-Originator-System", self.originator_addr)
        if self.request_dlr:
            e.add_header("X-Mms-Delivery-Report", "Yes")
        if self.request_rrr:
            e.add_header("X-Mms-Read-Reply", "Yes")
#        e.add_header("X-Mms-Originator-R/S-Delivery-Report", self.request_rrr)
        if tx.message.show_sender >= 0:
            e.add_header("X-Mms-Sender-Visibility", "Show" if tx.message.show_sender == 1 else "Hide")
        e.add_header("X-Mms-Forward-Counter", "1")
#        e.add_header("X-Mms-Previously-sent-by", self.x)
#        e.add_header("X-Mms-Previously-sent-date-and-time", self.x)
        if tx.message.content_adaptation >= 0:
            e.add_header("X-Mms-Adaptation-Allowed", "Yes" if tx.message.content_adaptation == 1 else "No")
        if tx.message.content_class:
            e.add_header("X-Mms-Content-Class", tx.message.content_class)
        if tx.message.drm >= 0:
            e.add_header("X-Mms-Drm-Content", "Yes" if tx.message.drm == 1 else "No")
        e.add_header("X-Mms-Message-ID", tx.message.message_id)
        if self.applic_id:
            e.add_header("X-Mms-Applic-ID", self.applic_id)
        if self.reply_applic_id:
            e.add_header("X-Mms-Reply-Applic-ID", self.reply_applic_id)
        if self.aux_applic_info:
            e.add_header("X-Mms-Aux-Applic-Info", self.aux_applic_info)
        if self.mmsip_addr:
            e.add_header("X-Mms-MMSIP-Address", self.mmsip_addr)
        if self.forward_route:
            e.add_header("X-Mms-Forward-Route", self.forward_route)
        if self.return_route:
            e.add_header("X-Mms-Return-Route", self.return_route)

        return e


    def send_to_mmsc(self, payload, txid, rcpt_from, mail_to):
        pl = payload.as_string()
        log.info("[{}] sending {} as MM4: {}{}"
            .format(self.gwid, txid, pl[:4096], ("..." if len(pl) > 4096 else ""))
        )
        if len(pl) > 4096:
            log.info("[{}] ...{}".format(self.gwid, pl[-256:]))
        smtp_err = ""
        try:
            self.connection.sendmail(rcpt_from, mail_to, pl)
            return None, ""
        except smtplib.SMTPRecipientsRefused as refused:
            log.info("[{}] {} all recipients in list {} were refused: {}"
                .format(gw.gwid, txid, mail_to, refused)
            )
            return "42", "SMTP error (all recipients were refused)"
        except smtplib.SMTPSenderRefused:
            log.info("[{}] {} sender {} refused".format(gw.gwid, txid, rcpt_from))
            return "41", "SMTP error (sender address refused)"
        except smtplib.SMTPException as smtpe:
            log.info("[{}] {} email not sent: {}".format(gw.gwid, txid, smtpe))
            return "40", "SMTP error (see gateway logs)"


    def _parse_address_list(self, cdl):
        if cdl is None: return ()
        return list(map(
            lambda e: email.utils.parseaddr(e)[1].split("@")[0].split("/")[0].replace("+", ""), 
            cdl.split(",")
        ))


    def process_mo(self, m):
        pass


    def process_mt_ack(self, m):
        txid = m['X-Mms-Message-Id'].replace("\"", "")
        log.debug("[{}] {} MT ack: {}".format(self.gwid, txid, m.as_string()))
        tx = models.transaction.MMSTransaction(txid)
        if tx is None:
            log.warning("[{}] transaction {} not found".format(self.gwid, txid))
            return None
        destinations = self._parse_address_list(m['X-Mms-Request-Recipients']) or \
            list(tx.destination | tx.cc | tx.bcc)
        log.info("[{}] {} ACK response for {}: {} {}"
            .format(self.gwid, txid, destinations, m['X-Mms-Request-Status-Code'], m['X-Mms-Status-Text'])
        )
        for d in destinations:
            status = "ACKNOWLEDGED" if m['X-Mms-Request-Status-Code'].lower() == "ok" else "FAILED"
            tx.set_state(d, status, m['X-Mms-Request-Status-Code'], m['X-Mms-Status-Text'], self.gwid)


    def process_dlr(self, m):
        txid = m['X-Mms-Message-Id']
        tx = models.transaction.MMSTransaction(txid)
        if tx is None:
            log.warning("[{}] transaction {} not found".format(self.gwid, txid))
            return None
        log.debug("[{}] processing MT DLR on transaction {}".format(self.gwid, txid))

        status = \
            "DELIVERED" if m['X-Mms-MM-Status-Code'].lower() in [ "retrieved" ] else \
            "FORWARDED" if m['X-Mms-MM-Status-Code'].lower() in [ "deferred", "indeterminate", "forwarded" ] else \
            "FAILED" if m['X-Mms-MM-Status-Code'].lower() in [ "expired", "rejected", "unrecognised", "unrecognized" ] else \
            "UNDEFINED"
        status_text = ((m['X-Mms-MM-Status-Extension'] or "") + " " + m['X-Mms-Status-Text']).strip()
        send_to_ua = m['X-Mms-Forward-To-Originator-UA'].lower() == "yes" if m['X-Mms-Forward-To-Originator-UA'] else False 
        tx.set_state(d, status, m['X-Mms-MM-Status-Code'], status_text, self.gwid, m['X-Mms-Transaction-ID'],
            extra={
                'send_to_UA': send_to_ua, 
                'app': m['X-Mms-Applic-ID'], 
                'reply_app': m['X-Mms-Reply-Applic-ID'], 
                'app_data': m['X-Mms-Aux-Applic-Info'] 
            }
        )
        log.info("[{}] MT DLR on {} processed successfully".format(self.gwid, txid))

        if m['X-Mms-Ack-Request'] is not None and m['X-Mms-Ack-Request'].lower() == "yes":
            if m.get('Sender') is None:
                log.warning("[{}] {} MT DLR confirmation requested, but no address to send it to".format(self.gwid, txid))
                return
            # provider asks for an ack on the DLR they sent
            log.info("[{}] {} MT DLR confirmation requested to {}".format(self.gwid, txid, m['Sender']))
            e = MIMEText("")
            e['From'] = self.originator_addr
            e['Sender'] = self.originator_addr
            e['To'] = m['Sender']
            e.add_header('X-Mms-3GPP-MMS-Version', self.protocol_version)
            e.add_header('X-Mms-Message-Type', "MM4_Delivery_report.RES")
            e.add_header('X-Mms-Transaction-ID', m['X-Mms-Transaction-ID'])
            e.add_header('X-Mms-Message-ID', m['X-Mms-Message-ID'])
            e.add_header('X-Mms-Request-Status-Code', "Ok")
            e.add_header('X-Mms-Status-Text', "Delivery report received")
            ret_code, ret_desc = self.send_to_mmsc(e, txid, self.originator_addr, m['Sender'])
            if ret_code is not None:
                log.warning("[{}] {} MT DLR confirmation failed: {}".format(self.gwid, txid, ret_desc))


    def process_rrr(self, m):
        pass
"""

X-Mms-Transaction-ID:
X-Mms-Request-Status-Code:
X-Mms-Status-Text:
"""

class MM7Gateway(MMSGateway):

    originator_system = None
    vaspid = None
    vasid = None
    service_code = None
    peer_timeout = 10

    def __init__(self, gwid):
        super(MM7Gateway, self).__init__(gwid)
        self.protocol = "MM7"


    def config(self, cfg):
        super(MM7Gateway, self).config(cfg)
        self.remote_peer[1] = int(cfg['outbound'].get('remote_port', 80 if self.secure else 443))
        self.originator_system = cfg['features'].get('originator_system')
        self.vaspid = cfg['gateway'].get('vaspid')
        self.vasid = cfg['gateway'].get('vasid')
        self.service_code = cfg['gateway'].get('service_code')
        self.peer_timeout = cfg['outbound'].get('timeout', 10.)


    def start(self):
        # start outbound gateway
        rdbq.set('gwstat-' + self.gwid, 1, 3 + GW_HEARTBEAT_TIMER)
        self.connection = self.remote_peer[0]
#        # register the gateway to receive inbound messages
#        rdbq.sadd('mmsrxsource-' + self.this_domain, self.gwid)
#        rdbq.sadd('mmsrxsource-' + self.this_host, self.gwid)
        return True


    def heartbeat(self):
        if rdbq.decr('gwstat-' + self.gwid) <= -1:
            # this gateway beeded to death, we need to stop the app
            rdbq.delete('gwstat-' + self.gwid)
            return False
        try:
            rp = requests.head(self.connection, auth=self.auth)
            if rp.ok:
                rdbq.set('gwstat-' + self.gwid, GW_HEARTBEATS,
                    ex=(GW_HEARTBEAT_TIMER * (2 + GW_HEARTBEATS))
                )
                log.debug("[{}] _/\_".format(self.gwid))
            else:
                log.critical("[{}] Remote server didn't responded {} to our HEAD request: {}"
                    .format(self.gwid, rp.status_code)
                )
            return rp.ok
        except requests.RequestException as re:
            log.critical("[{}] Gateway heartbeat failed: {}".format(self.gwid, re))
            return False


    def _add_address(self, tag, addr, prefix, suffix):
        if len(addr) == 0:
            return
        elif "@" in addr:
            ET.SubElement(tag, "RFC2822Address").text = addr
        elif addr.isdigit() and len(addr) < 7:
            ET.SubElement(tag, "ShortCode").text = prefix + addr + suffix
        elif addr.isdigit():
            ET.SubElement(tag, "number").text = prefix + addr + suffix
            

    def render(self, tx):

        env = ET.Element(MM7_ROOT_NS + ":Envelope", {
            'xmlns:' + MM7_ROOT_NS: MM7_NAMESPACE['env'],
        })
        env_header = ET.SubElement(env, MM7_ROOT_NS + ":Header")
        ET.SubElement(env_header, "mm7:TransactionID", {
            'xmlns:mm7': MM7_NAMESPACE['mm7'],
            MM7_ROOT_NS + ':mustUnderstand': "1",
        }).text = tx.tx_id
        env_body = ET.SubElement(env, MM7_ROOT_NS + ":Body")
        submit_rq = ET.SubElement(env_body, "SubmitReq", { 'xmlns': MM7_NAMESPACE['mm7'] })
        ET.SubElement(submit_rq, "MM7Version").text = MM7_VERSION['mm7']

        sender = ET.SubElement(submit_rq, "SenderIdentification")
        ET.SubElement(sender, "VASPID").text = self.vaspid
        ET.SubElement(sender, "VASID").text = self.vasid
        self._add_address(ET.SubElement(sender, "SenderAddress"), tx.message.origin, 
            self.origin_prefix, self.origin_suffix
        )

        recipients = ET.SubElement(submit_rq, "Recipients")
        if len(tx.destination):
            to = ET.SubElement(recipients, "To")
            for a in tx.destination:
                self._add_address(to, a, self.dest_prefix, self.dest_suffix)
        if len(tx.cc):
            cc = ET.SubElement(recipients, "Cc")
            for a in tx.cc:
                self._add_address(cc, a, self.dest_prefix, self.dest_suffix)
        if len(tx.bcc):
            bcc = ET.SubElement(recipients, "Bcc")
            for a in tx.bcc:
                self._add_address(bcc, a, self.dest_prefix, self.dest_suffix)

        if self.service_code:
            ET.SubElement(submit_rq, "ServiceCode").text = self.service_code
        if tx.linked_id:
            ET.SubElement(submit_rq, "LinkedID").text = tx.linked_id
        ET.SubElement(submit_rq, "MessageClass").text = tx.message.message_class
        ET.SubElement(submit_rq, "TimeStamp").text = datetime.datetime.now().isoformat()
#        if self.replycharging:
#            rc = ET.SubElement(submit_rq, "ReplyCharging")
#            rc.set("replyChargingSize", reply_charging_size)
#            rc.set("replyDeadline", self.reply_deadline or,
#                (datetime.datetime.now() + datetime.timedelta(days=10)).isoformat()
#            ))
        if tx.message.earliest_delivery > 0:
            ET.SubElement(submit_rq, "EarliestDeliveryTime").text = \
                datetime.datetime.fromtimestamp(float(tx.message.earliest_delivery)).isoformat()
        if tx.message.expire_after > 0:
            ET.SubElement(submit_rq, "ExpiryDate").text = \
                datetime.datetime.fromtimestamp(float(tx.message.expire_after)).isoformat()
        ET.SubElement(submit_rq, "DeliveryReport").text = "true" if self.request_dlr else "false"
        ET.SubElement(submit_rq, "ReadReply").text = "true" if self.request_rrr else "false"
        ET.SubElement(submit_rq, "Priority").text = tx.priority
        if tx.message.subject:
            ET.SubElement(submit_rq, "Subject").text = tx.message.subject
        if tx.message.charged_party:
            ET.SubElement(submit_rq, "ChargedPartyID").text = tx.message.charged_party
        if tx.message.can_redistribute:
            ET.SubElement(submit_rq, "DistributionIndicator").text = \
                "true" if tx.message.can_redistribute == 1 else "false"
        if tx.message.content_class:
            ET.SubElement(submit_rq, "ContentClass").text = tx.message.content_class
        if tx.message.drm:
            ET.SubElement(submit_rq, "DRMContent").text = "true" if tx.message.drm == 1 else "false"
        ET.SubElement(submit_rq, "Content", {
            'href': "cid:" + tx.tx_id + ".content",
            'allowAdaptations': "true" if tx.message.content_adaptation == 1 else "false",
        })
        env_str = '<?xml version="1.0"?>' + ET.tostring(env)
        log.debug("envelope: {}".format(env_str))

        top_part = MIMEMultipart('related', boundary=TOP_PART_BOUNDARY)

        env_part = email.message.Message()
        env_part.set_type("text/xml")
        env_part.add_header("Content-ID", tx.tx_id + ".envelope")
        env_part.set_payload(env_str)
        top_part.attach(env_part)

        content_part = MIMEMultipart()   # defaults to 'mixed'
        content_part.add_header("Content-ID", tx.tx_id + ".content")
        first_part_id = ""

        for pid in tx.message.parts:
            p = models.message.MMSMessagePart(pid)
            if p.content:
                # actual content is provided in the part object itself
                content = p.content
            elif (
                p.content_url.startswith("file://") or
                p.content_url.startswith("http://") or
                p.content_url.startswith("https://")
            ):
                # download the media file, unless already exists
                if not os.path.exists(content):
                    content = download_to_file(p.content_url, repo(
                        TMP_MEDIA_DIR, tx.message.message_id + "-" + p.content_id
                    ))
            else:
                log.warning("[{}] {} failed to obtain content for part '{}' in message {}"
                    .format(self.gwid, tx.tx_id, p.content_id, tx.message.message_id)
                )
                continue
            log.debug("[{}] {} message {} part {} saved as '{}'"
                .format(self.gwid, tx.tx_id, tx.message.message_id, p.content_id, content)
            )
            mp = None
            try:
                if p.content_type == "application/smil":
                    mp = MIMEBase("application", "smil", name=p.attachment_name)
                    mp.set_payload(content)
                elif p.content_type == "text/plain":
                    mp = MIMEText(content)
                elif p.content_type.startswith("image/"):
                    fh = open(content)
                    mp = MIMEImage(fh.read())
                    fh.close()
                elif p.content_type.startswith("audio/"):
                    fh = open(content)
                    mp = MIMEAudio(fh.read())
                    fh.close()
            except Exception as e:
                log.warning("[{}] {} failed to create MIME part '{}' component for message {}: {}"
                    .format(self.gwid, tx.tx_id, p.content_id, tx.message.message_id, e)
                )
                mp = None
            if mp:
                mp.add_header("Content-Id", p.content_id)
                content_part.attach(mp)
                if len(first_part_id) == 0:
                    content_part.set_param("start", p.content_id)
                    first_part_id = p.content_id

        top_part.attach(content_part)

        return top_part


    def send_to_mmsc(self, payload, txid, from_addr=None, to_addrs=None):

        headers = {
            'SOAPAction': "\"\"",
            'Content-Type': "multipart/related; " +
                "boundary=\"" + TOP_PART_BOUNDARY + "\"; " +
                "start=\"" + txid + ".envelope\""
        }
        content_lines = payload.as_string().splitlines()
        content = ""; content_started = False
        for l in content_lines:
            # seek to the first occurence of the top part boundary
            content_started = content_started or (l == "--" + TOP_PART_BOUNDARY)
            if content_started and not l.startswith("MIME-Version:"):
                content += l + "\r\n"
        log.debug("[{}] sending {} MM7 with headers: {}"
            .format(self.gwid, txid, headers)
        )
        log.debug("[{}] {} content: {}{}"
            .format(self.gwid, txid, content[:4096], ("..." if len(content) > 4096 else ""))
        )
        if len(content) > 4096:
            log.debug("[{}] ... {}".format(self.gwid, content[-256:]))
        try:
            rp = requests.post(self.remote_peer[0],
                auth=self.auth,
                headers=headers,
                data=content,
                timeout=self.peer_timeout
            )
            log.info("[{}] {} response status {}: {}"
                .format(self.gwid, txid, rp.status_code, rp.text)
            )
            if rp.ok:
                 # bad habit: MM7 does 200 OK responses, but indicate an error
                 try:
                     env = xmltodict.parse(rp.text)
                 except Exception as e:
                     log.warning("[{}] {} failed to xml-parse the SOAP envelope in MM7 response: {}"
                         .format(self.gwid, txid, e)
                     )
                     return "51", "failed to xml-parse the SOAP envelope in MM7 response"

                 stat = find_in_dict(env, 'Status')
                 if stat:
                     status_code = stat.get('StatusCode', "0")
                     status_text = stat.get('StatusText', "")
                     if status_code in [ "1000", "1100" ]:
                         return None, find_in_dict(env, 'MessageID')
                     else: 
                         return status_code, status_text 
                 else:
                     return "52", "message type not identified in MM7 response"
            else:
                 return rp.status_code, rp.reason
        except requests.RequestException as rqe:
            log.info("[{}] {} http error: {}".format(gw.gwid, txid, rqe))
            return "50", "http error ({})".format(rqe)


    @classmethod
    def build_response(cls, message_type, transaction, msgid, status):
        env = ET.Element("soapenv:Envelope", { 'xmlns:soapenv': MM7_NAMESPACE['env'] })
        env_header = ET.SubElement(env, "soapenv:Header")
        ET.SubElement(env_header, "mm7:TransactionID", {
            'xmlns:mm7': MM7_NAMESPACE['mm7'],
            'soapenv:mustUnderstand': "1",
        }).text = transaction
        env_body = ET.SubElement(env, "soapenv:Body")
        rp = ET.SubElement(env_body, message_type, { 'xmlns': MM7_NAMESPACE['mm7'] })
        ET.SubElement(rp, "MM7Version").text = VERSION['mm7']
        stat = ET.SubElement(rp, "Status")
        ET.SubElement(stat, "StatusCode").text= status
        ET.SubElement(stat, "StatusText").text = MM7_STATUS[status]
        if msgid:
            ET.SubElement(rp, "MessageID").text = msgid
        soap = '<?xml version="1.0" ?>' + ET.tostring(env)
        log.info("Prepared response: {}".format(soap))
        return soap


    def create_message(self, x):
        pass


    def process_dlr_metadata(meta):
        log.debug("DLR metadata elements: {}".format(list(meta)))
        msgid = meta.findtext("./{" + MM7_NAMESPACE['mm7'] + "}MessageID", "").strip()
        mt_status = meta.findtext("./{" + MM7_NAMESPACE['mm7'] + "}MMStatus", "").strip()
        # process Sender and Recipients/To|Cc|Bcc tags, with Number|ShortCode|RFC2822Address subtags
        return msgid, None


    def process_rr_metadata(meta):
        msgid = meta.findtext("./{" + MM7_NAMESPACE['mm7'] + "}MessageID", "").strip()
        return msgid, None


    def process_cancel_metadata(meta):
        msgid = meta.findtext("./{" + MM7_NAMESPACE['mm7'] + "}MessageID", "").strip()
        return msgid, None


    def process_replace_metadata(meta):
        msgid = meta.findtext("./{" + MM7_NAMESPACE['mm7'] + "}MessageID", "").strip()
        return msgid, None


