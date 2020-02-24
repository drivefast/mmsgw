import uuid
import time
import bottle
import rq
import requests

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

    tx.nq(tj.get('gateway'))
    tx.save()
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
    last_req_id = None
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

    def __init__(self, txid=None, msgid=None):
        if txid is None:
            self.tx_id = str(uuid.uuid4()).replace("-", "")
            self.last_req_id = str(uuid.uuid4()).replace("-", "")
            self.created_ts = int(time.time())
            self.message = models.message.MMSMessage(msgid)
            self.save()
            rdb.expireat('mmstx-' + self.tx_id, int(time.time()) + MMSTX_TTL)
        else:
            self.load(txid)


    def save(self):
        rdb.hmset('mmstx-' + self.tx_id, {
            'message_id': self.message.message_id,
            'last_req_id': self.last_req_id,
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
        })


    def load(self, txid):
        txd = rdb.hgetall("mmstx-" + txid)
        if txd:
            self.tx_id = txid
            self.last_req_id = txd['last_req_id']
            self.message = models.message.MMSMessage(txd['message_id'])
            self.gateway_id = txd['gateway_id']
            self.gateway = txd['gateway']
            l = txd.get('destination', "")
            self.destination = set(l.split(",")) if len(l) > 0 else set()
            l = txd.get('cc', "")
            self.cc = set(l.split(",")) if len(l) > 0 else set()
            l = txd.get('bcc', "")
            self.bcc = set(l.split(",")) if len(l) > 0 else set()
            self.linked_id = txd['linked_id']
            self.priority = txd['priority']
            self.carrier_ref = txd['carrier_ref']
            self.report_url = txd['report_url']
            self.created_ts = txd['created_ts']
            self.processed_ts = txd['processed_ts']


    def to_dict(self):
        return {
            'transaction_id': self.tx_id,
            'last_req_id': self.last_req_id,
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
        log.info("[{}] transmission {} queued for processing".format(gateway, self.tx_id))
        self.set_state([], "SCHEDULED")


    def set_state(self, dest, state, err="", desc="", gwid="", provider_id=None, extra=None):

        s = {
            "state": state,
            "code": err,
            "description": desc,
            "gateway": gwid,
            "timestamp": int(time.time())
        }
        if provider_id is not None:
            s['provider_id'] = provider_id
        if extra:
            s.update(extra)

        if len(dest):
            for d in dest:
                k = "mmstx-stat-" + self.tx_id + "-" + d
                rdb.hmset(k, s)
                rdb.expireat(k, int(time.time()) + MMSTX_TTL)
        else:
            k = "mmstx-stat-" + self.tx_id
            rdb.hmset(k, s)
            rdb.expireat(k, int(time.time()) + MMSTX_TTL)

        # callback to the app if necessary
        s['transaction_id'] = self.tx_id
        if len(dest):
            s['destinations'] = dest
        q_cb = rq.Queue("QCB", connection=rdbq)
#        url_list = self.report_url.split(",") + gw.report_url.split(",")
#        for url in url_list:
#            q_cb.enqueue_call(func='models.transaction.send_event', args=( url, s ))


def send_event(url, content):
    rp = requests.post(url, json=content)

