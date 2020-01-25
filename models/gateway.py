import time
import datetime
import uuid
import base64
import rq
import os.path
import shutil

import traceback

import xml.etree.cElementTree as ET
import smtplib
import email
import mimetypes
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.audio import MIMEAudio

from constants import *
from backend.logger import log
from backend.util import download_to_file, repo
from backend.storage import rdbq
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
            m = gw.render(tx)
            log.debug("[{}] {} prepared for transmission".format(gw.gwid, txid))
            ret = gw.send_to_mmsc(m, tx)
            log.info("[{}] {} sent to reciptients: {}".format(gw.gwid, txid, ret))
            # if any recipients not in this list, we should point out to them
            #! adjust transmission status
        except smtplib.SMTPRecipientsRefused as refused:
            log.info("[{}] {} all reciptients were refused: {}".format(gw.gwid, txid, refused))
        except smtplib.SMTPSenderRefused as refused:
            log.info("[{}] {} refused sender {}".format(gw.gwid, txid, tx.message.origin))
        except Exception as ex:
            print(traceback.format_exc())
            log.info("[{}] {} gateway error: {}".format(gw.gwid, txid, ex))
            reschedule(this_job, gw.q_tx)


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
    q_ev = None

    # gateway
    name = None
    group = None
    protocol = None
    protocol_version = None
    carrier = ""
    active = True
    tps_limit = 0

    # outbound
    secure = False
    remote_peer = None       # SMTP remote server for MM4, as ( host, port ) tuple; MMSC URL for MM7
    auth = None              # authentiation to use for transmitting messages, as ( user, password ) tuple
    local_host = None        # identification of the local server
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
    originator_system = None              # MM4 only
    originator_recipient_address = None   # MM4 only
    mmsip_address = None                  # MM4 only
    forward_route = None                  # MM4 only
    return_route = None                   # MM4 only


    def __init__(self, gwid):
        self.gwid = gwid
        self.group = gwid.split(":")[0]
        self.q_tx = rq.Queue("QTX-" + self.group, connection=rdbq)
        self.q_ev = rq.Queue("QEV-" + self.group, connection=rdbq)
        self.q_rx = rq.Queue("QRX-" + self.group, connection=rdbq)


    def config(self, cfg):
        self.group = cfg['gateway'].get('group')
        self.name = cfg['gateway'].get('name')
        self.potocol_version = cfg['gateway'].get('version')
        self.carrier = cfg['gateway'].get('carrier', "")
        self.tps_limit = int(cfg['gateway'].get('tps_limit', 0))

        self.secure = cfg['outbound'].get('secure_connection', "").lower() in ("yes", "true", "t", "1")
        self.remote_peer = [ cfg['outbound'].get('remote_host', "localhost"), 0 ]
        if len(cfg['outbound'].get('username', "")) > 0 and len(cfg['outbound'].get('password', "")) > 0:
            self.auth = ( cfg['outbound']['username'], cfg['outbound']['password'] )
        self.local_host = cfg['outbound'].get('local_host', "")
        
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
    server = None


    def __init__(self, gwid):
        super(MM4Gateway, self).__init__(gwid)
        self.protocol = "MM4"


    def config(self, cfg):
        super(MM4Gateway, self).config(cfg)
        self.remote_peer[1] = int(cfg['outbound'].get('remote_port', (465 if self.secure else 25)))
        self.originator_system = cfg['features'].get('originator_system')
        self.originator_recipient_address = cfg['features'].get('originator_recipient_address')
        self.mmsip_address = cfg['features'].get('mmsip_address')
        self.forward_route = cfg['features'].get('forward_route')
        self.return_route = cfg['features'].get('return_route')
        keyfile = cfg['outbound'].get('keyfile')
        certfile = cfg['outbound'].get('certfile')
        if keyfile is not None and certfile is not None and self.secure:
            self.ssl_certificate = ( keyfile, certfile )
        self.this_domain = cfg['inbound'].get('domain')
        self.this_host = cfg['inbound'].get('host')


    def connect(self):
        # try connecting to the remote MMSC we will use for sending MTs
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
            # this gateway beeded to death, we need to stop the app
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

        log.debug("[{}] building MM4 message".format(tx.tx_id))
        e = MIMEMultipart("related", boundary=TOP_PART_BOUNDARY)

        e['From'] = self.origin_prefix + tx.message.origin + self.origin_suffix
        e['Sender'] = self.origin_prefix + tx.message.origin + self.origin_suffix
        e['Subject'] = tx.message.subject
        e['To'] = ",".join(map(lambda a: self.dest_prefix + a + self.dest_suffix, tx.destination))
        e['Cc'] = ",".join(map(lambda a: self.dest_prefix + a + self.dest_suffix, tx.cc))
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
                log.debug("[{}] {} failed to create MIME part '{}' component for message {}: {}"
                    .format(self.gwid, tx.tx_id, p.content_id, tx.message.message_id, e)
                )
                mp = None
            if mp:
                mp.add_header("Content-Id", p.content_id)
                e.attach(mp)

        e.add_header("X-Mms-3GPP-MMS-Version", self.protocol_version)
        e.add_header("X-Mms-Message-Type", "MM4_forward.REQ")
        e.add_header("X-Mms-Transaction-ID", tx.tx_id)
        if tx.message.expire_after:
            e.add_header("X-Mms-Expiry", tx.message.expire_after)
        if tx.message.message_class:
            e.add_header("X-Mms-Message-Class", tx.message.message_class)
        if tx.priority:
            e.add_header("X-Mms-Priority", tx.priority)
        if self.request_ack:
            e.add_header("X-Mms-Ack-Request", "1")
        if self.request_dlr:
            e.add_header("X-Mms-Delivery-Report", "1")
        if self.request_rrr:
            e.add_header("X-Mms-Read-Reply", "1")
#        e.add_header("X-Mms-Originator-R/S-Delivery-Report", self.request_rrr)
        if tx.message.show_sender >= 0:
            e.add_header("X-Mms-Sender-Visibility", str(tx.message.show_sender))
        e.add_header("X-Mms-Forward-Counter", "1")
#        e.add_header("X-Mms-Previously-sent-by", self.x)
#        e.add_header("X-Mms-Previously-sent-date-and-time", self.x)
        if tx.message.content_adaptation >= 0:
            e.add_header("X-Mms-Adaptation-Allowed", str(tx.message.content_adaptation))
        if tx.message.content_class:
            e.add_header("X-Mms-Content-Class", tx.message.content_class)
        if tx.message.drm >= 0:
            e.add_header("X-Mms-Drm-Content", str(tx.message.drm))
        e.add_header("X-Mms-Message-ID", tx.message.message_id)
        if self.applic_id:
            e.add_header("X-Mms-Applic-ID", self.applic_id)
        if self.reply_applic_id:
            e.add_header("X-Mms-Reply-Applic-ID", self.reply_applic_id)
        if self.aux_applic_info:
            e.add_header("X-Mms-Aux-Applic-Info", self.aux_applic_info)
        if self.originator_system:
            e.add_header("X-Mms-Originator-System", self.originator_system)
        if self.originator_recipient_address:
            e.add_header("X-Mms-Originator-Recipient-Address", self.originator_recipient_address)
        if self.mmsip_address:
            e.add_header("X-Mms-MMSIP-Address", self.mmsip_address)
        if self.forward_route:
            e.add_header("X-Mms-Forward-Route", self.forward_route)
        if self.return_route:
            e.add_header("X-Mms-Return-Route", self.return_route)

#        return e.as_string()
        log.debug(e.as_string())
        return e


    def send_to_mmsc(self, payload, transmission):
#        return self.connection.sendmail(
#            self.origin_prefix + transmission.message.origin + self.origin_suffix,
#            (
#                list(map(lambda a: self.dest_prefix + a + self.dest_suffix, transmission.destination)) +
#                list(map(lambda a: self.dest_prefix + a + self.dest_suffix, transmission.cc)) +
#                list(map(lambda a: self.dest_prefix + a + self.dest_suffix, transmission.bcc))
#            ),
#            payload
#        )
        return self.connection.send_message(payload)


class MM7Gateway(MMSGateway):

    connection = None
    server = None

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
        self.peer_timeout = cfg['outbound'].get('timeout', 10),


    def _add_address(tag, addr, prefix, suffix):
        if len(addr):
            return
        elif "@" in addr:
            ET.SubElement(tag, "RFC2822Address").text = addr
        elif addr.isdigit() and len(addr) < 7:
            ET.SubElement(tag, "ShortCode").text = prefix + addr + suffix
        elif tx.message.origin.isdigit():
            ET.SubElement(tag, "number").text = prefix + addr + suffix
            

    def heartbeat(self):
        pass


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
        if tx.message.earliest_delivery:
            ET.SubElement(submit_rq, "EarliestDeliveryTime").text = \
                datetime.datetime.from_timestamp(tx.message.earliest_delivery).isoformat()
        if tx.message.expire_after:
            ET.SubElement(submit_rq, "ExpiryDate").text = \
                 datetime.datetime.from_timestamp(tx.message.expire_after).isoformat()
        ET.SubElement(submit_rq, "DeliveryReport").text = self.request_dlr
        ET.SubElement(submit_rq, "ReadReply").text = self.request_rrr
        ET.SubElement(submit_rq, "Priority").text = tx.priority
        if tx.message.subject:
            ET.SubElement(submit_rq, "Subject").text = tx.message.subject
        if tx.message.charged_party:
            ET.SubElement(submit_rq, "ChargedPartyID").text = tx.message.charged_party
        if tx.message.distribution_indicator:
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

        for p in tx.message.message.parts:
            content = None
            if p.content:
                content = p.content
            elif p.content_url.startswith("@"):
                content = p.content_url
            else:
                fn = download_to_file(p.content_url,
                    TEMPORARY_FILES_DIR + tx.message.message_id + "-" + p.content_id
                )
                if fn:
                    content = "@" + fn
                    # we also may wanna save this with the message object, so we
                    # don't need to download the file again if the transmission 
                    # fails; but is it wise to do so, since the file is ephemeral?
            if content is None:
                continue
            mp = None
            try:
                if p.content_type == "application/smil":
                    mp = MIMEBase("application", "smil", name=p.attachment_name)
                    mp.set_payload(content)
                elif p.content_type == "text/plain":
                    mp = MIMEText(content)
                elif p.content_type.startswith("image/"):
                    fh.open(content[1:])
                    mp = MIMEImage(fh.read())
                    fh.close()
                elif p.content_type.startswith("audio/"):
                    fh.open(content[1:])
                    mp = MIMEAudio(fh.read())
                    fh.close()
            except Exception as e:
                log.debug("")
                mp = None
            if mp:
                mp.add_header("Content-Id", p.content_id)
                content_part.attach(mp)
                if len(first_part_id) == 0:
                    content_part.set_param("start", p.content_id)
                    first_part_id = p.content_id

        top_part.attach(content_part)

        return top_part


    def send_to_mmsc(self, payload, transmission):

        headers = {
            'SOAPAction': "\"\"",
            'Content-Type': "multipart/related; " +
                "boundary=\"" + TOP_PART_BOUNDARY + "\"; " +
                "start=\"" + transmission.tx_id + ".envelope\""
        }
        content_lines = payload.as_string().splitlines()
        content = ""; content_started = False
        for l in content_lines:
            # seek to the first occurence of the top part boundary
            content_started = content_started or (l == "--" + TOP_PART_BOUNDARY)
            if content_started and not l.startswith("MIME-Version:"):
                content += l + "\r\n"
        log.debug("sending request headers: {} -- content {} ...".format(headers, content[:4096]))
        if len(content) > 4096:
            log.debug("... {}".format(content[-256:]))
        rp = requests.post(self.remote_peer[0],
            auth=self.auth,
            headers=headers,
            data=content,
            timeout=self.peer_timeout
        )
        log.info("response status {}: {}".format(rp.status_code, rp.text))
        return "response status {}: {}\n".format(rp.status_code, rp.text)


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


