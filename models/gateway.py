import time
import rq
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart

from backend.storage import rdbq
from models.transaction import MMSTransaction
from models.message import MMSMessage


def send_mms(txid):
    tx = MMSTransaction(txid)
    if tx is None:
        log.warning("[{}] Transaction not found when attempting to send".format(txid))
        return
    gw = MMSGateway(tx.gateway_id)
    m = gw.render(tx)
    gw.transmit(m)


class MMSGateway(object):

    gwid = None
    q_tx = None
    q_rx = None
    q_ev = None

    # gateway
    group = None
    protocol = None
    protocol_version = None
    carrier = ""
    active = True
    tps_limit = 0
    vaspid = None  # MM7 only
    vasid = None   # MM7 only

    # outbound
    secure = False
    remote_peer = None       # SMTP remote server for MM4, as ( host, port ) tuple; MMSC URL for MM7
    auth = None              # authentiation to use for transmitting messages, as ( user, password ) tuple
    conn_timeout = 0
    local_host = None        # identification of the local server
    ssl_certificate = None   # ( keyfile, certfile ) tuple, MM4 only

    # inbound
    peer_domain = None   # emails coming from any of these domains will be dispatched to this gateway
    peer_host = None

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

    
    @classmethod
    def group(cls, group, task_type="TX"):
        gateways = []
        all_gateways = Worker.all_keys(connection=rdbq)
        for gwid in all_gateways:
            gw = rdbq.hgetall("gw-" + gwid + "-" + task_type)
            if gw is not None and gw_worker['group'] == group:
                gateways.append(gw)
        return gateways;


    @classmethod
    def dispatch(cls, group, task_type):
        gateways = []
        gw_last = None
        gw_load = {}
        gw_last_used = {}
        all_gateways = Worker.all_keys(connection=rdbq)
        for gwid in all_gateways
            gw = rdbq.hgetall("gw-" + gwid + "-" + task_type)
            if gw is not None and gw_worker['group'] == group:
                gateways.append(gwid)
                if gw['last_selected'] == 1:
                    rdbq.hmset("gw-" + gwid + "-" + task_type, 'last_selected', 0)
                    gw_last = gwid
                gw_load[gwid] = gw['n_tasks']
                gw_last_used[gwid] = gw['last_task_ts']

        if len(gateways) == 0:
            return None
        gwid = ""
        if GW_GROUP_DISTRIBUTION == "RR":
            # get alphabetically ordered list of gwid's, select first after last selected, move last_selected
            gwids = sorted(gateways)
            if gw_last is None:
                gwid = gateways[0]
            else:
                for gwix in range(len(gwids)):
                    if gwids[gwix] == gw_last:
                        gwid = gwids[(1 + gwix) % len(gwids)]
                        break
        elif GW_GROUP_DISTRIBUTION == "PREF":
            # select first gateway in alphabetically ordered list of gateway IDs
            gwid = sorted(gateways)[0]
        elif GW_GROUP_DISTRIBUTION == "LU":
            # select the gateway that had the least tasks assigned
            gw_lu = sorted(gw_load.items(), key=lambda(k): k[1])
            gwid = gw_lu[0][0]
        elif GW_GROUP_DISTRIBUTION == "LRU":
            # select the gateway that as the last to get assigned a task
            gw_lru = sorted(gw_last_used.items(), key=lambda(k): k[1])
            gwid = gw_lru[0][0]
        else:
            # select an arbitrary gateway from the group
            gwid = random.choice(gateways())

        # adjust gateway records in group, after selecting this gateway
        rdbq.hmset("gw-" + gwid + "-" + task_type, {
            'last_selected': 1,
            'last_task_ts': int(time.time(),
        })
        rdbq.hincrby("gw-" + gwid + "-" + task_type, 'n_tasks', 1)

        return gwid


    def __init__(self, gwid):
        self.gwid = gwid
        self.q_tx = rq.Queue("QTX-" + self.gwid, connection=rdbq)
        self.q_ev = rq.Queue("QEV-" + self.gwid, connection=rdbq)
        self.q_rx = rq.Queue("QRX-" + self.gwid, connection=rdbq)


    def config(self, cfg):
        self.potocol_version = cfg['gateway'].get('version')
        self.carrier = cfg['gateway'].get('carrier', "")
        self.tps_limit = int(cfg['gateway'].get('tps_limit', 0))

        self.secure = cfg['outbound'].get('secure_connection', "").lower() in ("yes", "true", "t", "1")
        self.remote_peer = ( cfg['outbound'].get('remote_host', "localhost"), 0 )
        self.auth = ( cfg['outbound'].get('username', ""), cfg['outbound'].get('password', "") )
        self.conn_timeout = int(cfg['outbound'].get('connection_timeout', 0))
        self.local_host = cfg['outbound'].get('local_host', "")
        
        self.dest_prefix = cfg['addressing'].get('dest_prefix', "").
        self.dest_suffix = cfg['addressing'].get('dest_suffix', "").
        self.origin_prefix = cfg['addressing'].get('origin_prefix', "").
        self.origin_suffix = cfg['addressing'].get('origin_suffix', "").

        self.request_ack = cfg['features'].get('request_submit_ack', "").lower() in ("yes", "true", "t", "1")
        self.request_dlr = cfg['features'].get('request_delivery', "").lower() in ("yes", "true", "t", "1")
        self.request_rrr = cfg['features'].get('request_read_receipt', "").lower() in ("yes", "true", "t", "1")

        self.applic_id = cfg['features'].get('applic_id')
        self.reply_applic_id = cfg['features'].get('reply_applic_id')
        self.aux_applic_info = cfg['features'].get('aux_applic_info')


    def register(group=None):
        if group:
            self.group = group
            for task_type in [ "RX", "TX", "EV" ]:
                rdbq.hmset("gw-" + self.gwid + "-" + task_type, {
                    'group': self.group,
                    'last_selected': 1,
                    'last_task_ts': 0,
                    'n_tasks': 0,
                })
                for gwid in MMSGateway.group(self.group, task_type).keys():
                    rdbq.hmset("gw-" + gwid + "-" + task_type, {
                        'last_selected': 0,
                        'last_task_ts': 0,
                        'n_tasks': 0,
                    })
                    

class MM4Gateway(MMSGateway):

    connection = None
    server = None


    def __init__(self, gwid):
        super(MMSGateway, self).__init__(gwid)
        self.protocol = "MM4"


    def config(self, cfg):
        super(MMSGateway, self).config(cfg)
        self.remote_peer[1] = int(cfg['outbound'].get('remote_port', 25 if self.secure else 465))
        self.originator_system = cfg['features'].get('originator_system')
        self.originator_recipient_address = cfg['features'].get('originator_recipient_address')
        self.mmsip_address = cfg['features'].get('mmsip_address')
        self.forward_route = cfg['features'].get('forward_route')
        self.return_route = cfg['features'].get('return_route')
        keyfile = cfg['outbound'].get('keyfile')
        certfile = cfg['outbound'].get('certfile')
        if keyfile is not None and certfile is not None and self.secure_connection:
            self.ssl_certificate = ( keyfile, certfile )
        self.peer_domain = cfg['inbound'].get('domain')
        self.peer_host = cfg['inbound'].get('host')


    def start(self):
        ok = True

        # outbound gateway
        try:
            if self.secure_connection:
                self.connection = smtplib.SMTP_SSL(
                    self.remote_peer[0], self.remote_peer[1], 
                    self.local_hostname, 
                    self.ssl_certificate[0], self.ssl_certificate[1],
                    self.conn_timeout
                )
            else:
                self.connection = smtplib.SMTP(
                    self.remote_peer[0], self.remote_peer[1], 
                    self.local_hostname, 
                    self.conn_timeout
                )
        except SMTPException as e:
            log.alarm(e.description)
            ok = False

        # register the gateway to receive inbound messages
        rdbq.sadd('mmsrxsource-' + self.peer_domain, self.gwid)
        rdbq.sadd('mmsrxsource-' + self.peer_host, self.gwid)

        return ok


    def render(self, tx):
        e = MIMEMultipart("related")

        e['From'] = self.origin_prefix + tx.message.origin + self.origin_suffix
        e['Sender'] = self.origin_prefix + tx.message.origin + self.origin_suffix
        e['Subject'] = tx.message.subject
        e['To'] = ",".join(tx.destination)
        e['Cc'] = ",".join(tx.cc)
        e['Bcc'] = ",".join(tx.bcc)
        
        for p in message.parts:
            content = p.content or download(p.content_url)
            if content = None:
                continue
            if p.content_type == "application/smil":
                mp = MIMEBase("application", "smil", name=p.attachment_name)
                mp.set_payload(content)
                mp.add_header("Content-Id", p.content_id)
                e.set_param("start", p.content_id)
                e.attach(mp)
            elif p.content_type == "text/plain":
                mp = MIMEText(content)
                mp.add_header("Content-Id", p.content_id)
                e.attach(mp)
            elif p.content_type.startswith("image/"):
                fh.open(content[1:])
                mp = MIMEImage(fh.read())
                fh.close()
                mp.add_header("Content-Id", p.content_id)
                e.attach(mp)
            elif p.content_type.startswith("audio/"):
                fh.open(content[1:])
                mp = MIMEAudio(fh.read())
                fh.close()
                mp.add_header("Content-Id", p.content_id)
                e.attach(mp)




class MM7Gateway(MMSGateway):

    connection = None
    server = None


    def __init__(self, gwid):
        super(MMSGateway, self).__init__(gwid)
        self.protocol = "MM7"


    def config(self, cfg):
        super(MMSGateway, self).config(cfg)
        self.remote_peer[1] = int(cfg['outbound'].get('remote_port', 80 if self.secure else 443))
        self.originator_system = cfg['features'].get('originator_system')
        self.vaspid = cfg['gateway'].get('vaspid')
        self.vasid = cfg['gateway'].get('vasid')



