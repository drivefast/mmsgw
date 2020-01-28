import uuid
import time
import bottle
import rq

from constants import *
from backend.logger import log
from backend.storage import rdb, rdbq
from backend.util import makeset
import models.message
import models.gateway


@bottle.post(URL_ROOT + "/mms_send/<msgid>")
def enqueue_mms_transaction(msgid):
    tj = bottle.request.json
    tx = MMSTransaction(msgid=msgid)

    tx.destination = makeset(tj.get("destination"))
    tx.cc = makeset(tj.get('cc'))
    tx.bcc = makeset(tj.get('bcc'))
    # make sure we have destination numbers
    if (len(tx.destination) + len(tx.cc) + len(tx.cc)) == 0:
        return json_error(400, "Bad request", "No destinations")

    tx.linked_id = tj.get('linked_id', "")
    pri = tj.get('priority', "").lower()
    tx.priority = pri if pri in ACCEPTED_MESSAGE_PRIORITIES else "normal"
    tx.report_url = tj.get('report_url', "")

    tx.save()
    tx.nq(tj.get('gateway'))
    return tx.to_dict()


# handle MM7 requests received from carrier side
@bottle.post(URL_ROOT + "/mm7/<gw_group>/incoming")
def mm7_incoming(gw_group):
    raw_content = bottle.request.body.read()
    log.info("{} request received, {} bytes".format(gw_group, bottle.request.headers['Content-Length']))
    log.debug("raw content: {}...".format(raw_content[:4096]))
    if len(raw_content) > 4096:
        log.debug("... {}".format(raw_content[-256:]))

    bottle.response.content_type = "text/xml"
    mime_headers = "Mime-Version: 1.0\nContent-Type: " + bottle.request.headers['Content-Type']

    # try parsing lightly, to determine what queue to place this in
    m = email.message_from_string(mime_headers + "\n\n" + raw_content)
    try:
        if m.is_multipart():
            parts = m.get_payload()
            log.debug("handling as multipart, {} parts".format(len(parts)))
            env_content = parts[0].get_payload(decode=True)
            log.debug("SOAP envelope: {}".format(env_content))
            env = ET.fromstring(env_content)
        else:
            log.debug("handling as single part")
            env = ET.fromstring(m.get_payload(decode=True))
    except ET.ParseError as e:
        log.warning("{} failed to xml-parse the SOAP envelope: {}".format(gw_group, e))
        return bottle.HTTPResponse(status=400, body="Failed to xml-parse the SOAP envelope")

    # get the transaction tag, treat it as unique ID of the incoming message
    transaction_tag = env.find(
        "./{" + MM7_NAMESPACE['env'] + "}Header/{" + MM7_NAMESPACE['mm7'] + "}TransactionID"
    , MM7_NAMESPACE)
    if transaction_tag is None:
        s = "SOAP envelope of received request invalid, at least missing a transaction ID"
        log.warning(s)
        return bottle.HTTPResponse(status=400, body=s)
    transaction_id = transaction_tag.text.strip()

    # try to identify the message type
    mo_meta = env.find(
        "./{" + MM7_NAMESPACE['env'] + "}Body/{" + MM7_NAMESPACE['mm7'] + "}DeliverReq"
    , MM7_NAMESPACE)
    if mo_meta:
        log.debug("Incoming message is an MO")
        q_rx = rq.Queue("QRX-" + gw_group, connection=rdbq)
        q_rx.enqueue_call(
            func='models.gateway.process_mo', args=( transaction_id, mo_meta, parts[1], ), 
            job_id=transaction_id,
            meta={ 'retries': MAX_RX_RETRIES },
            ttl=30
        )
        return MM7Gateway.build_response("DeliverRsp", transaction_id, msgid, '1000')

    dlr_meta = env.find(
        "./{" + MM7_NAMESPACE['env'] + "}Body/{" + MM7_NAMESPACE['mm7'] + "}DeliveryReportReq"
    , MM7_NAMESPACE)
    if dlr_meta:
        log.debug("Incoming message is a DLR")
        q_ev = rq.Queue("QEV-" + gw_group, connection=rdbq)
        q_ev.enqueue_call(
            func='models.gateway.process_event', args=( transaction_id, dlr_meta, ), 
            job_id=transaction_id,
            meta={ 'retries': MAX_EV_RETRIES },
            ttl=30
        )
        return MM7Gateway.build_response("DeliveryReportRsp", transaction_id, msgid, '1000')

    # handling for other MM7 requests (cancel, replace, read-reply, etc) go here 

    log.warning("Unknown or unhandled message type")
    return bottle.HTTPResponse(status=400, body="Unknown or unhandled message type")


class MMSTransaction(object):

    tx_id = None
    message = None
    gateway = ""
    gateway_id = ""
    destination = set()
    cc = set()
    bcc = set()
    linked_id = ""
    priority = ""
    carrier_ref = ""
    report_url = ""
    created_ts = 0
    processed_ts = 0
    rendered_ts = 0
    sent_ts = 0
#    forwarded_ts = 0
#    forwarded_status = ""
#    delivered_ts = 0
#    delivered_status = ""
#    read_reply_ts = 0
#    read_reply_status = ""
    final_status = ""

    def __init__(self, txid=None, msgid=None):
        if txid is None:
            self.tx_id = str(uuid.uuid4()).replace("-", "")
            self.created_ts = int(time.time())
            self.message = models.message.MMSMessage(msgid)
            self.save()
            rdb.expireat('mmstx-' + self.tx_id, int(time.time()) + MMSTX_TTL)
        else:
            self.load(txid)


    def save(self):
        rdb.hmset('mmstx-' + self.tx_id, {
            'message_id': self.message.message_id,
            'gateway': self.gateway,
            'gateway_id': self.gateway_id,
            'destination': ",".join(self.destination),
            'cc': ",".join(self.cc),
            'bcc': ",".join(self.bcc),
            'linked_id': self.linked_id,
            'priority': self.priority,
            'carrier_ref': self.carrier_ref,
            'report_url': self.report_url,
            'created_ts': self.created_ts,
            'processed_ts': self.processed_ts,
            'rendered_ts': self.rendered_ts,
            'sent_ts': self.sent_ts,
#            'forwarded_ts': self.forwarded_ts,
#            'forwarded_status': self.forwarded_status,
#            'delivered_ts': self.delivered_ts,
#            'delivered_status': self.delivered_status,
#            'read_reply_ts': self.read_reply_ts,
#            'read_reply_status': self.read_reply_status,
            'final_status': self.final_status,
        })


    def load(self, txid):
        tx = rdb.hgetall('mmstx-' + txid)
        if tx:
            self.tx_id = txid
            self.message = models.message.MMSMessage(tx['message_id'])
            self.gateway_id = tx['gateway_id']
            self.gateway = tx['gateway']
            l = tx.get('destination', "")
            self.destination = set(l.split(",")) if len(l) > 0 else set()
            l = tx.get('cc', "")
            self.cc = set(l.split(",")) if len(l) > 0 else set()
            l = tx.get('bcc', "")
            self.bcc = set(l.split(",")) if len(l) > 0 else set()
            self.linked_id = tx['linked_id']
            self.priority = tx['priority']
            self.carrier_ref = tx['carreir_ref']
            self.report_url = tx['report_url']
            self.created_ts = tx['created_ts']
            self.processed_ts = tx['processed_ts']
            self.rendered_ts = tx['rendered_ts']
            self.sent_ts = tx['sent_ts']
#            self.forwarded_ts = tx['forwarded_ts']
#            self.forwarded_status = tx['forwarded_status']
#            self.delivered_ts = tx['delivered_ts']
#            self.delivered_status = tx['delivered_status']
#            self.read_reply_ts = tx['read_reply_ts']
#            self.read_reply_status = tx['read_reply_status']
            self.final_status = tx['final_status']


    def to_dict(self):
        return {
            'transaction_id': self.tx_id,
            'message_id': self.message.message_id,
            'gateway_id': self.gateway_id,
            'gateway': self.gateway,
            'destination': list(self.destination),
            'cc': list(self.cc),
            'bcc': list(self.bcc),
            'linked_id': self.linked_id,
            'priority': self.priority,
            'carrier_ref': self.carrier_ref,
            'report_url': self.report_url,
            'created_ts': self.created_ts,
            'processed_ts': self.processed_ts,
            'rendered_ts': self.rendered_ts,
            'sent_ts': self.sent_ts,
#            'forwarded_ts': self.forwarded_ts,
#            'forwarded_status': self.forwarded_status,
#            'delivered_ts': self.delivered_ts,
#            'delivered_status': self.delivered_status,
#            'read_reply_ts': self.read_reply_ts,
#            'read_reply_status': self.read_reply_status,
            'final_status': self.final_status,
        }


    def nq(self, gateway):
        self.gateway = gateway
        q_tx = rq.Queue("QTX-" + (gateway or DEFAULT_GATEWAY), connection=rdbq)
        q_tx.enqueue_call(
            func='models.gateway.send_mms', args=( self.tx_id, ), 
            job_id=self.tx_id,
            meta={ 'retries': MAX_TX_RETRIES },
            ttl=30
        )
        log.debug("[{}] transmission queued on gateway {}".format(self.tx_id, gateway))

